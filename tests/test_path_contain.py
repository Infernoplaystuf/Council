"""
Containment checks read both paths the same way (path_contain).

DataIndex.safe_write_path refused every write under a vault reached through
its 8.3 short name: Path.resolve() turned the write root into its long name,
and the file inside it into \\\\?\\<long name>\\... — realpath keeps that
prefix once the long result passes 260 characters — so the two never
compared equal. These tests make a real short name (GetShortPathNameW) under
a folder whose long name is past that length, and skip where the volume makes
no 8.3 names.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

import data_index
import path_contain

windows_only = pytest.mark.skipif(os.name != "nt", reason="Windows paths")


def _short_name(long_path: Path) -> str:
    """The 8.3 spelling of an existing folder, or '' when there is none."""
    import ctypes
    from ctypes import wintypes
    fn = ctypes.windll.kernel32.GetShortPathNameW
    fn.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    fn.restype = wintypes.DWORD
    buf = ctypes.create_unicode_buffer(32768)
    n = fn("\\\\?\\" + str(long_path), buf, 32768)
    short = buf.value[4:] if buf.value.startswith("\\\\?\\") else buf.value
    if not n or short.lower() == str(long_path).lower():
        return ""
    return short


#: The vault's long name is this long: its data_out (+9) stays under 260
#: characters, a file in data_out/dream3d (+26) does not — the case where
#: realpath prefixed one side and not the other.
_LONG_VAULT = 240


@pytest.fixture
def short_vault(tmp_path):
    """(short spelling, long spelling) of a vault whose long name is
    _LONG_VAULT characters."""
    if os.name != "nt":
        pytest.skip("8.3 short names are a Windows thing")
    left = _LONG_VAULT - len(str(tmp_path))
    if left < 40:
        pytest.skip("the temp folder is already too deep for this test")
    parts = []
    while left > 1:                     # each part costs its length + a "\"
        n = min(60, left - 1)
        parts.append(("longvaultfolder" * 5)[:n])
        left -= n + 1
    long = tmp_path.joinpath(*parts)
    assert len(str(long)) == _LONG_VAULT
    os.makedirs("\\\\?\\" + str(long), exist_ok=True)
    short = _short_name(long)
    if not short or len(short) > 200:
        pytest.skip("this volume makes no usable 8.3 short names")
    try:
        yield Path(short), long
    finally:
        # Past 260 characters only the \\?\ spelling can delete it.
        import shutil
        shutil.rmtree("\\\\?\\" + str(tmp_path / parts[0]),
                      ignore_errors=True)


@windows_only
def test_a_vault_reached_by_its_short_name_can_be_written(short_vault):
    short, long = short_vault
    idx = data_index.DataIndex(search_roots=[data_index.input_dir(short)],
                               write_root=data_index.output_dir(short))
    out = idx.safe_write_path("task_x.py", subfolder="dream3d")
    out.write_text("x", encoding="utf-8")
    # It landed in the vault's own output area, whichever spelling is used.
    assert path_contain.is_under(out, long / "data_out" / "dream3d")
    assert os.path.exists("\\\\?\\" + str(long / "data_out" / "dream3d"
                                          / "task_x.py"))


@windows_only
def test_the_short_name_does_not_weaken_containment(short_vault, tmp_path):
    short, long = short_vault
    out_root = data_index.output_dir(short)
    (out_root).mkdir(parents=True, exist_ok=True)
    idx = data_index.DataIndex(search_roots=[data_index.input_dir(short)],
                               write_root=out_root)
    # Only the basename is used; a subfolder cannot climb out.
    p = idx.safe_write_path("..\\..\\evil.txt", subfolder="..\\..")
    assert path_contain.is_under(p, long / "data_out")
    # The vault root, a sibling with the same prefix, and an input are out.
    assert not path_contain.is_under(short, out_root)
    assert not path_contain.is_under(Path(str(short) + "\\data_out_old\\x"),
                                     out_root)
    assert not path_contain.is_under(data_index.input_dir(short) / "a.csv",
                                     out_root)


@windows_only
def test_a_junction_out_of_the_output_area_is_outside_it(tmp_path):
    import _winapi
    vault = tmp_path / "vault"
    out = vault / "data_out"
    out.mkdir(parents=True)
    (vault / "notes.docx").write_text("mine", encoding="utf-8")
    link = out / "looks_inside"
    _winapi.CreateJunction(str(vault), str(link))
    try:
        assert not path_contain.is_under(link / "notes.docx", out)
        assert not data_index.is_under(link / "notes.docx", out)
        assert path_contain.is_under(out / "real.txt", out)
    finally:
        os.rmdir(link)                  # the link only, never its target
    assert (vault / "notes.docx").read_text(encoding="utf-8") == "mine"


def test_dot_dot_and_prefix_siblings_are_not_inside(tmp_path):
    root = tmp_path / "data_out"
    root.mkdir()
    assert path_contain.is_under(root, root)
    assert path_contain.is_under(root / "a" / ".." / "b.txt", root)
    assert not path_contain.is_under(root / ".." / "b.txt", root)
    assert not path_contain.is_under(tmp_path / "data_out_old" / "x", root)
    assert not path_contain.is_under(None, root)
    assert not path_contain.is_under(root, None)


@windows_only
def test_unc_and_stream_names_are_recognised(tmp_path):
    root = tmp_path / "data_out"
    root.mkdir()
    assert not path_contain.is_under("\\\\server\\share\\x.dream3d", root)
    assert not path_contain.is_under("\\\\?\\UNC\\server\\share\\x", root)
    assert path_contain.is_under("\\\\?\\" + str(root / "x.dream3d"), root)
    assert path_contain.has_stream_name(str(root / "x.txt:hidden"))
    assert path_contain.has_stream_name("\\\\?\\C:\\a:b")
    assert not path_contain.has_stream_name(str(root / "x.txt"))
    assert not path_contain.has_stream_name("C:\\a\\b.txt")


@windows_only
def test_run_folder_accepts_an_output_dir_under_a_short_name_vault(
        short_vault, monkeypatch):
    import nx_bridge
    short, _long = short_vault
    sent = {}
    monkeypatch.setattr(nx_bridge, "run_job",
                        lambda job, **_k: sent.update(job) or {"ok": 0})
    out = data_index.output_dir(short) / "dream3d" / "runs"
    out.mkdir(parents=True)     # an existing folder resolves to \\?\<long>
    nx_bridge.run_folder("p.d3dpipeline", short, out, vault_dir=short)
    assert sent["action"] == "run_folder"
    with pytest.raises(nx_bridge.NxError, match="outside the vault output"):
        nx_bridge.run_folder("p.d3dpipeline", short, short / "elsewhere",
                             vault_dir=short)
