"""
tests/run_all.py — the whole suite, one test file per process, several at once.

    python tests/run_all.py                 # every file, 4 at a time
    python tests/run_all.py -j 8 --timeout 600
    python tests/run_all.py tests/test_ledger_review.py tests/test_kg_contents.py

Run serially in one process the suite stalls for hours: Qt state, child
processes and module-level caches pile up from one file to the next. Each file
in its own `python -m pytest` keeps every file's start clean and lets four of
them share the machine. CI (.github/workflows/tests.yml) runs exactly this, so
a green run here is a green run there.

A file that runs past --timeout is stopped and counted as failed, with the
last lines of its output, so one hang cannot hold the run. Logs go to
--logs (default: a temp folder), one per file; the summary at the end lists
every file that failed with the tail of its log. Exit status is 0 only when
every file passed.

Standard library only, so it runs before anything else is installed.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parent.parent
SUITES = ("tests", "inferno_local/tests")
TAIL_LINES = 40


def test_files(roots: Sequence[str] = SUITES) -> List[Path]:
    files: List[Path] = []
    for r in roots:
        # pytest's own default: test_*.py and *_test.py (smoke_test.py)
        files.extend(sorted(set((ROOT / r).glob("test_*.py"))
                            | set((ROOT / r).glob("*_test.py"))))
    # Slowest first would be better; largest first is a fair stand-in and
    # keeps a long file from starting last and stretching the run.
    return sorted(files, key=lambda p: -p.stat().st_size)


def run_one(path: Path, logs: Path, timeout: int) -> Tuple[Path, str, float]:
    """(file, "passed" | "failed" | "timeout", seconds)."""
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", COUNCIL_NO_DIALOGS="1")
    log = logs / (path.relative_to(ROOT).as_posix().replace("/", "__") + ".log")
    start = time.monotonic()
    with open(log, "w", encoding="utf-8", errors="replace") as out:
        proc = subprocess.Popen(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
             str(path.relative_to(ROOT))],
            cwd=ROOT, env=env, stdout=out, stderr=subprocess.STDOUT)
        try:
            rc = proc.wait(timeout=timeout)
            # 5 = no tests collected (a file whose tests all skip at import).
            status = "passed" if rc in (0, 5) else "failed"
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            status = "timeout"
    return path, status, time.monotonic() - start


def tail(log: Path, n: int = TAIL_LINES) -> str:
    try:
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-n:])


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("files", nargs="*", help="test files (default: all)")
    ap.add_argument("-j", "--jobs", type=int, default=4)
    ap.add_argument("--timeout", type=int, default=900,
                    help="seconds one file may take (default 900)")
    ap.add_argument("--logs", default="", help="folder for the per-file logs")
    args = ap.parse_args(argv)
    # A Windows console is cp1252: one replacement character in a failing
    # file's log would end the summary with a UnicodeEncodeError.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass

    files = ([Path(f).resolve() for f in args.files] if args.files
             else test_files())
    logs = Path(args.logs or tempfile.mkdtemp(prefix="council-tests-"))
    logs.mkdir(parents=True, exist_ok=True)
    print(f"{len(files)} test files, {args.jobs} at a time; logs in {logs}",
          flush=True)

    results: List[Tuple[Path, str, float]] = []
    began = time.monotonic()
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futures = [pool.submit(run_one, f, logs, args.timeout) for f in files]
        for fut in as_completed(futures):
            path, status, secs = fut.result()
            results.append((path, status, secs))
            mark = "ok  " if status == "passed" else status.upper()
            print(f"[{len(results):3}/{len(files)}] {mark} {secs:6.1f}s "
                  f"{path.relative_to(ROOT).as_posix()}", flush=True)

    bad = [r for r in results if r[1] != "passed"]
    for path, status, _ in sorted(bad):
        name = path.relative_to(ROOT).as_posix()
        log = logs / (name.replace("/", "__") + ".log")
        print(f"\n===== {status.upper()}: {name} =====\n{tail(log)}")
    print(f"\n{len(results) - len(bad)} passed, {len(bad)} failed, "
          f"in {time.monotonic() - began:.0f}s")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
