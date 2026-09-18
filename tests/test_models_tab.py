"""
Models — the tab that froze for six seconds and then showed nothing useful.

Two kinds of defect here. One inherited: `hardware_detect.detect()` is called
inline in the Tk constructor and takes four to six seconds, which Tk hides
behind the splash and Qt cannot because it builds tabs lazily.

The rest were mine, and they are the reason most of these tests read a REAL
catalog entry rather than a fixture I invented: I wrote `find()` against a
signature and a set of field names I had assumed, and it produced an empty
table — and then a table where every model claimed to be CPU-only on a machine
with an RTX 4070.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("COUNCIL_NO_DIALOGS", "1")

from council_core import model_jobs as mj  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

#: One REAL catalog entry, copied from model_finder's own output. Using the
#: real shape is the whole point: an invented fixture agreed with my invented
#: field names and proved nothing.
REAL_ENTRY = {
    "id": "granite-3.1-8b-q4",
    "name": "IBM Granite 3.1 8B Instruct (Q4_K_M)",
    "org": "IBM", "role": "general", "params_b": 8.0, "quant": "Q4_K_M",
    "size_gb": 4.9, "context_k": 128, "vram_gb_q4": 6.5,
    "hf_repo": "bartowski/granite-3.1-8b-instruct-GGUF",
    "hf_file": "granite-3.1-8b-instruct-Q4_K_M.gguf",
    "license": "Apache-2.0", "origin": "us", "origin_verified": True,
    "fits_vram": True, "source": "catalog",
}


def test_the_module_imports_no_toolkit():
    source = (ROOT / "council_core" / "model_jobs.py").read_text(
        encoding="utf-8")
    for toolkit in ("tkinter", "PySide6", "PyQt5", "messagebox"):
        assert toolkit not in source


# ============================================================
# The field names I got wrong
# ============================================================

def test_a_real_catalog_entry_fills_every_column():
    """I invented `vram_gb`, `ctx_k`, `fits_gpu`, `repo` and `file`. The real
    keys are `vram_gb_q4`, `context_k`, `fits_vram`, `hf_repo` and `hf_file`,
    so every one of those columns rendered "—" and every model claimed to be
    CPU-only on a machine with an RTX 4070."""
    row = mj.to_row(REAL_ENTRY)
    assert row.cells[0] == "IBM Granite 3.1 8B Instruct (Q4_K_M)"
    assert row.cells[1] == "IBM"
    assert row.cells[2] == "8"
    assert row.cells[3] == "6.5", "the VRAM column is empty again"
    assert row.cells[4] == "yes", "a model that fits VRAM is marked CPU-only"
    assert row.cells[5] == "128", "the context column is empty again"


def test_no_column_is_silently_blank_for_a_real_entry():
    """A dash in every numeric column is what an invented field name looks
    like, and it looks like missing data rather than a bug."""
    row = mj.to_row(REAL_ENTRY)
    assert "—" not in row.cells, f"a column came back empty: {row.cells}"


def test_the_download_source_is_carried():
    row = mj.to_row(REAL_ENTRY)
    assert row.repo == "bartowski/granite-3.1-8b-instruct-GGUF"
    assert row.filename.endswith(".gguf")


def test_a_missing_value_shows_a_dash_rather_than_none():
    row = mj.to_row({"name": "Something"})
    assert row.cells[2] == "—"
    assert "None" not in "".join(row.cells)


# ============================================================
# US origin is verified for some rows and guessed for others
# ============================================================

def test_the_catalogs_own_verification_flag_is_used():
    """`origin_verified` is the catalog's claim. An online hit is classified
    by a NAME HEURISTIC and does not carry it — so a front end that treats
    both the same turns an inference into a promise."""
    assert mj.to_row(REAL_ENTRY).verified_origin is True
    online = dict(REAL_ENTRY, source="online", origin_verified=False)
    assert mj.to_row(online).verified_origin is False


def test_a_mixed_result_says_how_many_were_verified(monkeypatch):
    import types
    monkeypatch.setitem(sys.modules, "model_finder", types.SimpleNamespace(
        find_models=lambda **kw: {
            "catalog": [dict(REAL_ENTRY)],
            "online": [dict(REAL_ENTRY, name="Guessed", source="online",
                            origin_verified=False)],
        }))
    result = mj.find(mj.Hardware(), online=True)
    assert "1 with verified US origin" in result.message
    assert "1 inferred from the name" in result.message


def test_a_catalog_only_result_does_not_hedge(monkeypatch):
    import types
    monkeypatch.setitem(sys.modules, "model_finder", types.SimpleNamespace(
        find_models=lambda **kw: {"catalog": [dict(REAL_ENTRY)], "online": []}))
    result = mj.find(mj.Hardware())
    assert result.message == "1 model(s)."


def test_online_results_are_left_out_unless_asked_for(monkeypatch):
    """The app is offline by design; the checkbox is the consent."""
    import types
    monkeypatch.setitem(sys.modules, "model_finder", types.SimpleNamespace(
        find_models=lambda **kw: {
            "catalog": [dict(REAL_ENTRY)],
            "online": [dict(REAL_ENTRY, name="Online one")],
        }))
    assert len(mj.find(mj.Hardware(), online=False).rows) == 1


def test_the_real_finder_returns_real_rows():
    """Against the actual model_finder, not a stand-in. This is the test that
    would have caught the invented signature immediately."""
    hardware = mj.detect_hardware()
    result = mj.find(hardware)
    assert result.ok, result.message
    assert result.rows, "the real catalog produced no rows"
    assert all(row.repo for row in result.rows)


# ============================================================
# The probe
# ============================================================

def test_detection_is_memoised():
    """A GPU is not hot-swapped mid-session, and a Qt tab is built whenever it
    is first SHOWN — so without this a user who closes and reopens the tab
    waits four to six seconds again, every time."""
    import time
    mj.detect_hardware()                   # warm
    start = time.perf_counter()
    mj.detect_hardware()
    assert time.perf_counter() - start < 0.05


def test_the_pending_line_says_something_is_happening():
    """Six seconds of a blank label is indistinguishable from a broken tab."""
    assert "detecting" in mj.PENDING_HARDWARE.lower()


def test_the_hardware_line_reads_naturally():
    hardware = mj.Hardware(gpu="RTX 4070", vram_gb=8.0, ram_gb=31.7)
    assert "RTX 4070" in hardware.summary
    assert "8.0 GB" in hardware.summary


def test_a_machine_with_no_gpu_says_so_rather_than_none():
    assert "no GPU detected" in mj.Hardware().summary
    assert "None" not in mj.Hardware().summary


def test_the_qt_tab_does_not_probe_in_its_constructor():
    """THE INHERITED DEFECT. The Tk tab calls hardware_detect.detect() inline
    in _build_model_finder_tab and gets away with it because every tab is
    built up front behind the splash."""
    from tests.source_checks import code_of
    source = (ROOT / "council_qt" / "tabs" / "models.py").read_text(
        encoding="utf-8")
    build = code_of(source, "_build")
    assert "detect" not in build, "the constructor probes the hardware"
    probe = code_of(source, "detect_hardware")
    assert "threading.Thread" in probe


# ============================================================
# The ampersands
# ============================================================

def test_all_three_download_captions_carry_an_ampersand():
    """Two of the three are applied at RUN time as the banner changes. A port
    that escapes only the strings present at build time fixes one of the three
    and looks finished."""
    assert "&" in mj.DOWNLOAD_IDLE
    assert "&" in mj.DOWNLOAD_NONE
    assert "&" in mj.download_caption("Granite 8B")


def test_the_caption_names_the_model_when_there_is_one():
    assert "Granite 8B" in mj.download_caption("Granite 8B")
    assert "no upgrade available" in mj.download_caption(None)


def test_the_qt_tab_escapes_the_re_caption():
    from tests.source_checks import code_of
    source = (ROOT / "council_qt" / "tabs" / "models.py").read_text(
        encoding="utf-8")
    suggest = code_of(source, "on_suggest")
    assert "amp(" in suggest, (
        "the run-time re-caption is not escaped, so it renders as "
        "'Download _switch'")


# ============================================================
# Downloading
# ============================================================

def test_space_is_checked_before_the_download_not_during(tmp_path):
    """A multi-gigabyte download that dies at 90% has cost the user real time
    and told them nothing they could have known first."""
    assert mj.check_space(tmp_path, 0.001) is None
    problem = mj.check_space(tmp_path, 10_000_000)
    assert problem and "Not enough room" in problem


def test_no_size_means_no_space_check(tmp_path):
    assert mj.check_space(tmp_path, None) is None


def test_the_confirmation_says_what_is_left_alone():
    text = mj.confirm_download_text("Granite 8B", 4.9)
    assert "4.9 GB" in text
    assert "existing model file is left where it is" in text


def test_a_model_with_no_repo_is_refused(tmp_path):
    row = mj.ModelRow(model_id="x", cells=("x",) * 7)
    result = mj.download_and_switch(row, tmp_path)
    assert not result.ok
    assert "no download source" in result.message


def test_a_download_that_lands_but_cannot_switch_says_where_the_file_is(
        tmp_path, monkeypatch):
    """Losing track of a multi-gigabyte file the user just waited for is a
    worse outcome than the switch failing."""
    import types
    target = tmp_path / "model.gguf"
    target.write_text("x")
    monkeypatch.setitem(sys.modules, "model_downloader", types.SimpleNamespace(
        download_gguf=lambda **kw: target))
    monkeypatch.setitem(sys.modules, "onboarding", types.SimpleNamespace(
        save_gguf_path=lambda *a, **k: (_ for _ in ()).throw(
            OSError("read-only vault"))))
    row = mj.to_row(REAL_ENTRY)
    result = mj.download_and_switch(row, tmp_path)
    assert not result.ok
    assert str(target) in result.message
    assert "Engine settings" in result.message


def test_progress_is_a_pure_string_builder():
    """It is called from inside the downloader's callback, on the worker.
    Anything that touched a widget there would be the defect this whole layer
    exists to prevent."""
    from tests.source_checks import code_of
    body = code_of(mj.progress_line)
    for widgetish in ("setText", "self.", "_to_ui"):
        assert widgetish not in body


def test_progress_reads_sensibly_with_and_without_a_total():
    assert "50 %" not in mj.progress_line(5 * 1024**2, 10 * 1024**2, "m")
    assert "(50%)" in mj.progress_line(5 * 1024**2, 10 * 1024**2, "m")
    assert "MB" in mj.progress_line(5 * 1024**2, None, "m")


def test_copy_gives_the_repo_and_the_file():
    """The app is offline by design; fetching it yourself is the supported
    path, so the clipboard has to carry enough to do that."""
    text = mj.copy_text(mj.to_row(REAL_ENTRY))
    assert "bartowski/granite-3.1-8b-instruct-GGUF" in text
    assert ".gguf" in text
