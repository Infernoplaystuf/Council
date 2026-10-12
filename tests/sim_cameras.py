"""
Picking the camera a test MEANS — the simulated one — and a PC without
either camera SDK, whatever this machine has.

WHY: the `pylon` env runs the Typhon/camera tests with PYLON_CAMEMU set
(pylon's emulated Baslers) and OpenEB on the path. Discovery then lists the
emulated Baslers FIRST, so a helper that took "the first row ending in
frame" connected to an emulated Basler and the test failed far from the
cause ("'BaslerDevice' object has no attribute 'state'"); and a test of "an
SDK that is not installed" found pypylon installed with cameras on it. These
pick by what the row says the camera is (cameras.SyntheticBackend labels its
cameras "Simulated ... camera") and hide the SDKs explicitly, so every file
passes in one pylon-env process with PYLON_CAMEMU=2 as in the council env.
"""
from __future__ import annotations

import sys
from typing import Any, Sequence

#: In every simulated camera's row ("2. simulated Simulated event camera
#: (SIM-2) · event") and in no real camera's.
SIMULATED = "Simulated"

#: The modules the two camera backends import (cameras.BaslerBackend.sdk,
#: cameras.EvkBackend.sdk).
CAMERA_SDKS = ("pypylon", "pypylon.pylon", "metavision_hal")


def is_simulated(row: Any, kind: str) -> bool:
    """A camera-list row of the simulated camera of `kind`."""
    text = str(row)
    return SIMULATED in text and text.endswith(kind)


def simulated_index(rows: Sequence[Any], kind: str = "frame") -> int:
    """The position of the simulated `kind` camera in a camera list."""
    for index, row in enumerate(rows):
        if is_simulated(row, kind):
            return index
    raise AssertionError(f"no simulated {kind} camera in {list(rows)}")


def simulated_row(rows: Sequence[Any], kind: str = "frame") -> str:
    """The simulated `kind` camera's row in a camera list."""
    return str(rows[simulated_index(rows, kind)])


def without_camera_sdks(monkeypatch: Any) -> None:
    """Neither pypylon nor the Metavision HAL importable for this test, as
    on a PC that has neither: `import` of each raises ImportError (a None in
    sys.modules), so the backends say what to install. Undone with the
    test (monkeypatch)."""
    for name in CAMERA_SDKS:
        monkeypatch.setitem(sys.modules, name, None)
