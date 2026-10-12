"""
The data index's own files are not the user's data, and are not kept in
data_in/.

Found in review (2026-10-12): DataIndex kept its column index
(.vault_col_index.json) and its warm-start cache (data_index_cache.pickle) IN
data_in/ — the folder the app's own README calls read-only — and discover()
returned the column index as a data file. So every refresh() profiled it,
counted that as a change, and rewrote both files; the dataset overview's
signature (file count, newest mtime) then changed on every turn, and the
overview was rebuilt each time: 35 of a warm turn's 39 seconds on a
279-file vault, with no model asked. The same two files were counted as the
user's ("182 file(s) … 1 .pickle") and listed in the overview. And the cache
was a pickle — loaded at start from a folder the user drops files into.
"""
from __future__ import annotations

import gzip
import json
import pickle
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

#: What older builds left in data_in/: still there on every existing vault.
OLD_SIDECARS = (".vault_col_index.json", "data_index_cache.pickle")


@pytest.fixture
def vault(tmp_path):
    vault = tmp_path / "vault"
    data_in = vault / "data_in"
    data_in.mkdir(parents=True)
    (vault / "data_out").mkdir()
    (data_in / "pumps.csv").write_text(
        "pump,bay,name\nP1,4,ORCHID\nP2,5,LILAC\n", encoding="utf-8")
    (data_in / "valves.json").write_text(
        json.dumps([{"valve": "V1", "name": "PEONY"}]), encoding="utf-8")
    return vault


def _index(vault):
    import data_index
    return data_index.DataIndex(
        search_roots=[data_index.input_dir(vault)],
        write_root=data_index.output_dir(vault))


def _snapshot(folder):
    return {p.relative_to(folder).as_posix(): p.stat().st_mtime_ns
            for p in folder.rglob("*") if p.is_file()}


def test_a_refresh_writes_nothing_into_data_in(vault):
    data_in = vault / "data_in"
    before = _snapshot(data_in)
    index = _index(vault)
    index.refresh()
    assert {p.name for p in index.discover()} == {"pumps.csv", "valves.json"}
    assert _snapshot(data_in) == before, "the index wrote into data_in/"


def test_a_second_refresh_with_nothing_changed_changes_nothing(vault):
    """Measured before: 3 of 3 refreshes re-profiled the column index and
    rewrote it and the cache."""
    index = _index(vault)
    index.refresh()
    everything = _snapshot(vault)
    profiled = []
    real = index._profile_file
    index._profile_file = lambda p: profiled.append(p.name) or real(p)
    index.refresh()
    index.refresh()
    assert profiled == []
    assert _snapshot(vault) == everything


def test_the_files_an_older_build_left_in_data_in_are_not_data(vault):
    data_in = vault / "data_in"
    (data_in / ".vault_col_index.json").write_text(
        json.dumps({"version": 1, "files": {}}), encoding="utf-8")
    (data_in / "data_index_cache.pickle").write_bytes(b"not data")
    index = _index(vault)
    assert not {p.name for p in index.discover()} & set(OLD_SIDECARS)

    import dataset_digest
    import vault_analyst
    counts = vault_analyst.folder_file_counts(data_in)
    assert counts["total"] == 2, counts
    assert ".pickle" not in counts["by_ext"], counts
    summary = vault_analyst.folder_data_summary(data_in, recursive=True)
    assert set(summary["file"]) == {"pumps.csv", "valves.json"}, summary
    digest = dataset_digest.build_digest(vault)
    assert ".vault_col_index" not in digest and "pickle" not in digest

    # Touching them (what every refresh did) is not a change to the data.
    sig = dataset_digest._signature(data_in)
    for name in OLD_SIDECARS:
        (data_in / name).write_bytes(b"rewritten")
    assert dataset_digest._signature(data_in) == sig


def test_the_overview_is_built_once_for_turns_that_change_nothing(
        vault, monkeypatch):
    """The dataset overview's cache held for a turn only when data_in did
    not change; the index's own rewrites changed it on every turn."""
    import dataset_digest
    monkeypatch.setattr(dataset_digest, "_CACHE", {"sig": None, "text": ""})
    builds = []
    real = dataset_digest.build_digest
    monkeypatch.setattr(dataset_digest, "build_digest",
                        lambda *a, **k: builds.append(1) or real(*a, **k))
    index = _index(vault)
    for _turn in range(3):
        index.refresh()                    # what each data question does
        assert dataset_digest.get_digest(vault)
    assert len(builds) == 1, builds


def test_no_pickle_is_read_or_written(vault, monkeypatch):
    """A pickle in data_in/ — a folder the user copies other people's files
    into — was unpickled at start (code runs on load). CLAUDE.md: no pickle."""
    (vault / "data_in" / "data_index_cache.pickle").write_bytes(
        pickle.dumps({"version": 2, "profiles": {}}))

    def refuse(*_a, **_k):
        raise AssertionError("a pickle was read or written")

    for name in ("load", "loads", "dump", "dumps"):
        monkeypatch.setattr(pickle, name, refuse)
    index = _index(vault)
    index.refresh()
    restored = _index(vault)                 # the next launch's warm start
    assert {p.name for p in restored._profiles} == {"pumps.csv",
                                                    "valves.json"}
    caches = [p for p in vault.rglob("*") if "cache" in p.name.lower()
              and p.suffix != ".pickle"]
    assert caches, "no warm-start cache was written"
    for cache in caches:
        with gzip.open(cache, "rt", encoding="utf-8") as fh:
            assert json.load(fh)["profiles"]


def test_a_damaged_cache_is_a_cold_start(vault):
    index = _index(vault)
    index.refresh()
    for cache in [p for p in vault.rglob("*") if "cache" in p.name.lower()
                  and p.suffix != ".pickle"]:
        cache.write_bytes(b"\x1f\x8b not really gzip")
    restored = _index(vault)
    assert restored._profiles == {}
    restored.refresh()
    assert {p.name for p in restored._profiles} == {"pumps.csv",
                                                    "valves.json"}
