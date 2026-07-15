"""
nx_policy.py — which DREAM3D-NX filters this app will never run.

Pure stdlib, imported by BOTH environments (the app side and the nx worker),
so the same rule is enforced wherever a pipeline can be executed.

Why this exists
---------------
The rest of Part B sanctions filters by PROVENANCE: "is this UUID in the
installed binary?". That is not sanction. It answers where a filter came from,
not what it can do — and two of the 289 filters in the installed package do
something no amount of path-guarding can contain:

  * Execute Process runs an arbitrary shell command (`arguments: str`).
  * Create Python Plugin and/or Filters writes Python code to disk.

Both were reachable end to end. Execute Process is not a reader or a writer, so
the runner rewrote none of its paths and the writer-containment check skipped
it entirely; a two-step Read + Execute Process pipeline ran to completion and
h_run_folder reported ok=1, failed=0 while the spawned command did its work.
`blocking` defaults to False, so the filter returns no errors and the app calls
it a clean run. Retrieval ranked Execute Process the #1 hit for "run a process",
so the model was handed the UUID to copy — no hallucination required. And the
shortlist is not a boundary: validate() indexes the whole catalog.

A spawned process is bound by none of this app's guarantees. It is not a
writer, so data_out containment does not apply to it; it does not need the
network, so air-gapping does not stop `del /s /q`; and it runs as the user, so
it can delete a database. That is the one thing this app must never be able to
do.

So capability is denied by UUID — the only stable key, since the JSON filter
name format drifts between versions.

This is a denylist, not an allowlist, deliberately: the other 287 filters are
domain operations on the data structure, and an allowlist over them would have
to be regenerated on every DREAM3D-NX update and would silently break real
pipelines. The two entries here are the capability outliers, and they are
outliers precisely because they escape the data structure.
"""
from __future__ import annotations

from typing import Dict, Optional

# uuid -> why it is refused (shown to the user; keep it plain).
DENIED_UUIDS: Dict[str, str] = {
    "fb511a70-2175-4595-8c11-d1b5b6794221":
        "'Execute Process' runs an arbitrary shell command. Nothing this app "
        "does can contain a spawned process: it is not a writer, so the "
        "output-area check does not bind it, and it runs with your account's "
        "full access to your files.",
    "1a35f50d-a9f5-9ea2-af70-5b9cf894e45f":
        "'Create Python Plugin and/or Filters' writes Python code to disk, "
        "which DREAM3D-NX can then load and run.",
}


def is_denied(uuid) -> bool:
    return str(uuid) in DENIED_UUIDS


def reason(uuid) -> Optional[str]:
    return DENIED_UUIDS.get(str(uuid))


def permitted_filters(catalog: dict) -> list:
    """The catalog's filters minus the capability outliers."""
    return [f for f in (catalog or {}).get("filters", [])
            if f.get("uuid") and not is_denied(f["uuid"])]
