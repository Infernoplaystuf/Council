"""
tools/typhon_plugins_timing.py, the bench behind P1 row 3 (plugin design
10.2): it runs inside the real Qt "Typhon (plugins)" app, offscreen, and
gives numbers. Checked here for working, never for the numbers — those
depend on the PC and are recorded in docs/typhon_plugins_branch.md.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import typhon_plugins_timing as timing  # noqa: E402


def test_one_run_measures_a_capture_in_the_built_app(tmp_path):
    """One short capture through the app's own Start and Stop, with a plugin
    on the rig's host fed every live frame on its own thread."""
    import run_example_gui as rex

    pdir = rex.build("typhon_plugins", project="timing",
                     vault_dir=tmp_path / "vault", target="qt")
    spec = {"timer": "default", "plugin": "noop", "seconds": 1.0,
            "fps": 60.0, "project": str(pdir),
            "folder": str(tmp_path / "runs")}
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen",
               COUNCIL_NO_DIALOGS="1")
    out = subprocess.run([sys.executable, str(ROOT / "tools" /
                                              "typhon_plugins_timing.py"),
                          "--child", json.dumps(spec)],
                         capture_output=True, text=True, timeout=300,
                         env=env, cwd=str(ROOT), encoding="utf-8")
    assert out.returncode == 0, out.stderr[-2000:]
    said = json.loads([line for line in out.stdout.splitlines()
                       if line.startswith("{")][-1])
    assert said["timer"] == "default" and said["plugin"] == "noop"
    assert 30 <= said["frames"] <= 90, said        # about 60 in one second
    assert said["saved"] == said["grabbed"] >= said["frames"]
    assert said["feed"]["delivered"] > 0 and said["faults"] == []
    for key in ("late", "late_2", "median_ms", "p99_ms", "max_ms",
                "read_late", "event_wait_1ms", "switch_interval_ms",
                "cpu_busy_pct", "logical_cpus"):
        assert key in said, key
    if os.name == "nt":
        # The PC's load is part of every result (P1 review: the same 5 ms
        # plugin made 0 or 512-544 frames late on the same laptop).
        assert 0.0 <= said["cpu_busy_pct"] <= 100.0
    assert "PC busy" in timing.row(said)
    assert len(list((tmp_path / "runs").glob("*_frame_*.png"))) == \
        said["saved"]
    assert timing.row(said).split()[:2] == ["default", "noop"]


def test_the_matrix_names_only_what_it_knows():
    assert set(timing.TIMERS) == {"default", "1ms", "switch", "period"}
    assert timing.PLUGINS["slow20cap"] == (20.0, 0.25)
    assert timing._percentile([1, 2, 3, 4, 5], 0.99) == 5
    assert timing._percentile([], 0.5) == 0.0


def test_the_pcs_load_is_read_from_its_cpu_times():
    # (idle, kernel incl. idle, user): 30 idle of 100 = 70 % busy.
    assert timing.cpu_busy_pct((0, 0, 0), (30, 60, 40)) == 70.0
    assert timing.cpu_busy_pct(None, (1, 2, 3)) is None
    assert timing.cpu_busy_pct((5, 5, 5), (5, 5, 5)) is None
    load = timing.Load()
    load.begin()
    # Busy for 0.2 s, not for a fixed amount of work: Windows moves its
    # CPU times on at its clock tick (about 15.6 ms), and a few ms of work
    # inside one tick read the same times twice, which is "cannot tell"
    # (None) — it failed that way once in the gate.
    deadline = time.perf_counter() + 0.2
    while time.perf_counter() < deadline:
        sum(range(1000))
    said = load.end()
    assert said["logical_cpus"] == os.cpu_count()
    if os.name == "nt":
        assert 0.0 <= said["cpu_busy_pct"] <= 100.0
