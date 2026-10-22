"""Every tab that reads files in bulk skips one that takes too long, goes on
with the rest, and leaves it skipped for the other tabs too
(council_core.file_guard; user 2026-10-22)."""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import source_extract as se  # noqa: E402
from council_core import file_guard as fg  # noqa: E402
from council_core import ledger_ingest as li  # noqa: E402
from council_core import ledger_records as lr  # noqa: E402
from ledger_fixtures import make_laser_log, make_template  # noqa: E402

LIMIT = 0.5


@pytest.fixture
def guard(tmp_path, monkeypatch):
    monkeypatch.setenv("COUNCIL_APP_DIR", str(tmp_path / "app"))
    monkeypatch.setenv("COUNCIL_FILE_TIME_LIMIT_S", str(LIMIT))
    monkeypatch.setattr(fg, "_recovered", False)
    release = threading.Event()
    yield release
    release.set()                       # the reads left behind finish


def stalls_on(name, real, release):
    """``real``, except that it hangs on the file called ``name``."""
    def reader(path, *a, **k):
        if Path(path).name == name:
            release.wait(60)
        return real(path, *a, **k)
    return reader


def test_the_ledger_skips_a_stuck_workbook_and_reads_the_rest(guard, tmp_path, monkeypatch):
    src = tmp_path / "src"
    src.mkdir()
    csv = Path(li.create_parent(make_template(tmp_path / "T.xlsx"), tmp_path / "l.csv").csv)
    make_laser_log(src / "good.xlsx")
    make_laser_log(src / "stuck.xlsx")
    monkeypatch.setattr(se, "load_workbook_grids",
                        stalls_on("stuck.xlsx", se.load_workbook_grids, guard))
    t0 = time.monotonic()
    ex = li.run_extract(csv, [src], use_images=False)
    assert time.monotonic() - t0 < 30
    text = ex.describe()
    assert "1 too slow / stalled the app" in text
    assert any("stuck.xlsx: skipped: took longer than 0.5 s" in p for p in ex.problems)
    assert [Path(e.path).name for e in fg.entries()] == ["stuck.xlsx"]
    # the next Extract does not wait for it at all
    t0 = time.monotonic()
    again = li.run_extract(csv, [src], use_images=False)
    assert time.monotonic() - t0 < LIMIT * 4
    assert any("Retry skipped files" in p for p in again.problems)


def test_the_table_ledger_skips_a_stuck_workbook(guard, tmp_path, monkeypatch):
    from test_ledger_records import creature_rows, make_sample
    from test_ledger_records import make_template as btb_template
    src = tmp_path / "s"
    src.mkdir()
    for name in ("Good.xlsx", "Stuck.xlsx"):
        make_sample(src / name, creature_rows("Greg", 3), ["Name", "Health"], True)
    csv = tmp_path / "T.csv"
    lr.create_ledger(btb_template(tmp_path / "T.xlsx"), csv)
    monkeypatch.setattr(lr, "read_book", stalls_on("Stuck.xlsx", lr.read_book, guard))
    run = lr.extract(csv, sorted(src.glob("*.xlsx")), use_cache=False)
    assert ("Stuck.xlsx", "slow") in run.open_failures
    assert {r.file for r in run.records} == {"Good.xlsx"}


def test_the_connections_graph_skips_it_and_what_the_ledger_skipped(guard, tmp_path,
                                                                    monkeypatch):
    from council_core import knowledge_graph as kgm
    v = tmp_path / "vault"
    (v / "data_in").mkdir(parents=True)
    for name in ("good.txt", "stuck.txt", "ledger_hit.txt"):
        (v / "data_in" / name).write_text("Owner: Carol Lee\nPart: AB-100\n",
                                          encoding="utf-8")
    fg.add(v / "data_in" / "ledger_hit.txt", "took longer than 180 s to read", "Ledger")
    monkeypatch.setattr(kgm, "read_records", stalls_on("stuck.txt", kgm.read_records, guard))
    stats = kgm.KnowledgeGraph(v).seed()
    bad = "\n".join(stats["unreadable"])
    assert "stuck.txt: skipped: took longer than 0.5 s" in bad
    assert "ledger_hit.txt: skipped: took longer than 180 s" in bad
    assert "good.txt" not in bad
    assert kgm.unreadable_breakdown(stats["unreadable"]) == {"too slow / stalled": 2}


def test_the_vault_indexes_skip_it(guard, tmp_path, monkeypatch):
    import data_index
    import vault_index
    v = tmp_path / "vault"
    v.mkdir()
    (v / "good.md").write_text("alpha beta", encoding="utf-8")
    (v / "stuck.md").write_text("gamma delta", encoding="utf-8")
    monkeypatch.setattr(vault_index, "_index_file",
                        stalls_on("stuck.md", vault_index._index_file, guard))
    idx = vault_index.VaultIndex(v)
    idx.rebuild(max_workers=1)
    assert any(k.endswith("good.md") for k in idx.records)
    assert not any(k.endswith("stuck.md") for k in idx.records)
    # the data index: a CSV that hangs is profiled as skipped, the rest read
    d = tmp_path / "data"
    d.mkdir()
    (d / "ok.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    (d / "stuck.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    real = data_index.DataIndex._profile_delimited

    def profile(self, path, prof, **k):
        if path.name == "stuck.csv":
            guard.wait(60)
        return real(self, path, prof, **k)
    monkeypatch.setattr(data_index.DataIndex, "_profile_delimited", profile)
    di = data_index.DataIndex(search_roots=[d], write_root=tmp_path / "out")
    di.refresh()
    profs = {p.name: p for p in di._profiles.values()}
    assert profs["ok.csv"].error == "" and "took longer than" in profs["stuck.csv"].error
    assert {Path(e.path).name for e in fg.entries()} == {"stuck.md", "stuck.csv"}


def test_the_retry_button(guard, tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from council_qt.widgets.skipped_files import RetrySkippedButton
    said = []
    btn = RetrySkippedButton(said.append)
    assert btn.text() == "Retry skipped files (0)" and not btn.isEnabled()
    f = tmp_path / "huge.xlsx"
    f.write_bytes(b"x")
    fg.add(f, "took longer than 180 s to read", "Ledger")
    assert btn.refresh() == 1 and btn.isEnabled()
    assert "huge.xlsx - took longer than 180 s to read (Ledger)" in btn.toolTip()
    btn.click()
    assert fg.entries() == [] and not btn.isEnabled()
    assert said == ["1 skipped file(s) will be read again on the next run."]
