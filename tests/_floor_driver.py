"""
Helper run by tests/test_analyst_floor.py IN A CHILD PROCESS: install the
analyst file fence (council_core.analyst_floor) exactly as the analyst child
does — before the operations run, with an audit hook that cannot be removed —
then attempt a battery of raw file operations and print, as a one-line JSON on
stdout, which the fence refused.

    python _floor_driver.py <repo_root> <job.json>

The result is printed, not written to a file: the fence is armed, and the
output file would itself be outside the allow-list.

job.json: {"read_only": [...], "read_write": [...], "never": [...],
           "ops": [["read", path], ["write", path], ["listdir", path],
                   ["scandir", path], ["remove", path],
                   ["rename", path, path2], ["mkdir", path]]}
printed: {"<i>": "OK" | "REFUSED: <reason>" | "ERR <type>: <msg>",
          "per_open_cached_us": float, "build_install_ms": float}
"""
import json
import os
import sys
import time


def main(argv):
    repo, job_path = argv[1], argv[2]
    sys.path.insert(0, repo)
    # The job file is read BEFORE the fence is armed (it is outside the
    # allow-list, as the model's code and call are read before the fence too).
    job = json.loads(open(job_path, encoding="utf-8").read())
    from council_core import analyst_floor
    t0 = time.perf_counter()
    floor = analyst_floor.install(read_only=job.get("read_only") or [],
                                  read_write=job.get("read_write") or [],
                                  never=job.get("never") or [])
    build_install_ms = (time.perf_counter() - t0) * 1000.0

    def do(op):
        kind = op[0]
        if kind == "read":
            with open(op[1], "rb") as fh:
                fh.read()
        elif kind == "write":
            with open(op[1], "w", encoding="utf-8") as fh:
                fh.write("x")
        elif kind == "listdir":
            os.listdir(op[1])
        elif kind == "scandir":
            list(os.scandir(op[1]))
        elif kind == "remove":
            os.remove(op[1])
        elif kind == "rename":
            os.rename(op[1], op[2])
        elif kind == "mkdir":
            os.mkdir(op[1])
        else:
            raise ValueError(f"unknown op {kind!r}")

    out = {}
    for i, op in enumerate(job.get("ops") or []):
        try:
            do(op)
            out[str(i)] = "OK"
        except analyst_floor.Refused as e:
            out[str(i)] = "REFUSED: " + str(e)
        except Exception as e:                            # noqa: BLE001
            out[str(i)] = f"ERR {type(e).__name__}: {e}"

    # Cost of one open: first (uncached realpath) vs repeated (cached).
    probe = job.get("probe_read")
    if probe and os.path.isfile(probe):
        # warm the cache with one read, then time many
        try:
            with open(probe, "rb") as fh:
                fh.read()
        except analyst_floor.Refused:
            pass
        n = 2000
        t = time.perf_counter()
        for _ in range(n):
            with open(probe, "rb") as fh:
                fh.read()
        out["per_open_cached_us"] = (time.perf_counter() - t) / n * 1e6
    out["build_install_ms"] = build_install_ms
    # Printed, not written: the fence is armed and every folder but the
    # allow-list is off-limits, stdout included would be a file open — but
    # sys.stdout is already open, so writing to it fires no audit event.
    sys.stdout.write("FLOOR_RESULT:" + json.dumps(out) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
