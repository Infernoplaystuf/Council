"""
example.webcam — any webcam OpenCV can open, as a Typhon camera.

This is the worked example of docs/typhon_camera_plugins.md. Copy this
folder, rename the id, and replace the OpenCV calls with your camera's SDK.

What a camera plugin is made of (plugin API 1.0):

  * WebcamPlugin  — the plugin object Typhon makes (the manifest's "entry").
                    It says whether the SDK is installed, lists the cameras
                    and opens one.
  * WebcamDevice  — one opened camera (a typhon_plugin_api.Device): its
                    sensor size and area, start, stop and read a frame.
  * WebcamSettings — the camera's settings (a typhon_plugin_api.Provider):
                    the rows of Typhon's settings tabs.

The rules a plugin keeps: import Typhon only through typhon_plugin_api (plus
the standard library, numpy and your SDK); never raise from available() for
a missing SDK; raise api.CameraError for anything the camera refuses.
"""
from __future__ import annotations

import time
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

import typhon_plugin_api as api

#: The cameras' family name. A plugin from someone else uses one WITH A
#: DOT, normally its own id, so it can never claim a built-in's presets.
BACKEND = "example.webcam"

#: How many camera numbers to try when scanning (0, 1, 2, ...). OpenCV
#: cannot list cameras, so each number is opened in turn.
MAX_CAMERAS = 4

#: What the user is told when OpenCV is missing.
INSTALL_HINT = ("OpenCV is not installed in Typhon's Python: run "
                "'python -m pip install opencv-python' with the Python "
                "Typhon runs under, then restart Typhon")

#: The pixel formats this example offers: grey (one channel) or colour.
MONO8 = "Mono8"
RGB8 = "RGB8"


def _import_cv2() -> Any:
    import cv2                                     # your camera's SDK
    return cv2


class WebcamPlugin:
    """The plugin object: one per Typhon window (design 3.3)."""

    #: Its cameras' CameraInfo.backend.
    backend = BACKEND

    #: The OpenCV module to use: None imports the real one on first use.
    #: Tests put a stand-in here (tests/test_webcam.py), so the plugin can
    #: be tested on a PC with no webcam.
    cv2: Any = None

    def __init__(self) -> None:
        self.ctx: Optional[api.PluginContext] = None

    # -- called by Typhon when the plugin is switched on and off ----------
    def activate(self, ctx: api.PluginContext) -> None:
        self.ctx = ctx

    def deactivate(self) -> None:
        self.ctx = None

    def _sdk(self) -> Any:
        if self.cv2 is None:
            self.cv2 = _import_cv2()
        return self.cv2

    # -- the camera plugin (typhon_plugin_api.CameraPlugin) -----------------
    def available(self) -> Tuple[bool, str]:
        """(ready?, what to install when not). Never raises: a PC without
        OpenCV still runs Typhon, and the camera scan says what to do."""
        try:
            self._sdk()
        except Exception:                                 # noqa: BLE001
            return False, INSTALL_HINT
        return True, ""

    def discover(self) -> List[Any]:
        """The webcams this PC has, WITHOUT keeping any open."""
        cv2 = self._sdk()
        found = []
        for number in range(MAX_CAMERAS):
            cap = cv2.VideoCapture(number)
            try:
                if not cap.isOpened():
                    continue
                found.append(api.CameraInfo(
                    backend=BACKEND, key=f"webcam-{number}",
                    model=f"Webcam {number}", serial=f"WEBCAM-{number}",
                    vendor="USB", kind="frame"))
            finally:
                cap.release()
        return found

    def open(self, info: Any) -> "WebcamDevice":
        if getattr(info, "backend", "") != BACKEND:
            raise api.CameraError(f"{getattr(info, 'label', info)} is not a "
                                  f"webcam")
        number = int(str(info.key).rsplit("-", 1)[-1])
        return WebcamDevice(info, self._sdk(), number)

    def capabilities(self, device: Any) -> FrozenSet[str]:
        """What THIS opened camera can do, never more than the manifest's
        "capabilities": Typhon reads the basics off the device's own flags
        (frames, restartable, ...)."""
        return frozenset(api.device_capabilities(device))

    def facet(self, device: Any, name: str) -> Any:
        """A webcam has no trigger and no binning: no facets."""
        return None

    def categories(self) -> api.CategoryHints:
        """Which settings tab each group goes in: one tab, "Camera"."""
        return api.CategoryHints(tips={"Camera": "The webcam's picture."},
                                 most_used={"Camera": ("PixelFormat",)})

    def record_fields(self, device: Any) -> Dict[str, Any]:
        """Plain JSON saved in each run's camera record."""
        width, height = device.sensor_size
        return {"camera_number": device.number, "width": width,
                "height": height}

    def review_source(self) -> Optional[api.ReviewSource]:
        """A webcam writes no file of its own: its saved frames are its
        data."""
        return None


class WebcamDevice(api.Device):
    """One opened webcam."""

    #: Stop then start on one connection is fine.
    restartable = True
    #: The area (crop) changes only with the stream stopped.
    area_live = False

    def __init__(self, info: Any, cv2: Any, number: int) -> None:
        self.info = info
        self.number = number
        self._cv2 = cv2
        self._cap = cv2.VideoCapture(number)
        if not self._cap.isOpened():
            raise api.CameraError(f"webcam {number} could not be opened — "
                                  f"is another program using it?")
        ok, picture = self._cap.read()
        if not ok or picture is None:
            self._cap.release()
            raise api.CameraError(f"webcam {number} opened but sent no "
                                  f"picture")
        height, width = picture.shape[:2]
        self.sensor_size = (int(width), int(height))
        self._roi = api.Roi(0, 0, int(width), int(height))
        self._started = False
        self._index = 0
        self.pixel_format = MONO8
        self._settings = WebcamSettings(self)

    # -- the sensor and its area ------------------------------------------
    def limits(self) -> Any:
        width, height = self.sensor_size
        return api.Limits(width=width, height=height)

    def roi(self) -> Any:
        return self._roi

    def set_roi(self, roi: Any) -> Any:
        """Crop the picture to `roi` (done here, in software: most webcams
        cannot crop on the sensor). Returns the area actually used."""
        if self.streaming:
            raise api.NeedsStop("roi")
        width, height = self.sensor_size
        if roi.empty:
            self._roi = api.Roi(0, 0, width, height)
            return self._roi
        x = min(max(0, int(roi.x)), width - 1)
        y = min(max(0, int(roi.y)), height - 1)
        w = max(1, min(int(roi.w), width - x))
        h = max(1, min(int(roi.h), height - y))
        self._roi = api.Roi(x, y, w, h)
        return self._roi

    # -- the stream --------------------------------------------------------
    def start(self) -> None:
        self._started = True

    def stop(self) -> None:
        self._started = False

    def read(self, timeout_ms: int = 1000) -> Optional[Any]:
        """The next frame, or None if none came inside the timeout. Called
        only from Typhon's grab thread. Returns within the timeout plus
        100 ms."""
        if not self._started:
            time.sleep(min(timeout_ms, 50) / 1000.0)
            return None
        ok, picture = self._cap.read()
        if not ok or picture is None:
            return None
        x, y, w, h = self._roi.as_tuple()
        picture = picture[y:y + h, x:x + w]
        cv2 = self._cv2
        if self.pixel_format == MONO8:
            picture = cv2.cvtColor(picture, cv2.COLOR_BGR2GRAY)
        else:
            picture = cv2.cvtColor(picture, cv2.COLOR_BGR2RGB)
        self._index += 1
        # The frame must OWN its array (a copy, never a view of a buffer
        # the SDK reuses): Typhon keeps it while it is saved and shown.
        import numpy as np
        image = np.array(picture, copy=True, order="C")
        return api.Frame(image=image, index=self._index,
                         timestamp_us=int(time.monotonic() * 1e6),
                         meta={"aoi": self._roi.as_tuple(),
                               "format": self.pixel_format})

    def close(self) -> None:
        self._started = False
        try:
            self._cap.release()
        except Exception:                                 # noqa: BLE001
            pass

    # -- settings ----------------------------------------------------------
    def settings_provider(self) -> Any:
        return self._settings


class WebcamSettings(api.Provider):
    """A webcam's settings, as the rows of Typhon's settings tabs. Each is
    an api.Setting; `read` and `write` talk to the camera, and Typhon's own
    `set` coerces, writes, reads back and says what happened."""

    def __init__(self, device: WebcamDevice) -> None:
        self.device = device

    def describe(self) -> List[Any]:
        width, height = self.device.sensor_size
        return [
            api.Setting(key="PixelFormat", label="Pixel format",
                        group="Camera", kind="choice",
                        value=self.device.pixel_format,
                        choices=(MONO8, RGB8), live=False,
                        help="Grey (Mono8) or colour (RGB8)."),
            api.Setting(key="Width", label="Sensor width", group="Camera",
                        kind="int", value=width, unit="px", read_only=True),
            api.Setting(key="Height", label="Sensor height", group="Camera",
                        kind="int", value=height, unit="px", read_only=True),
        ]

    def read(self, key: str) -> Any:
        width, height = self.device.sensor_size
        values = {"PixelFormat": self.device.pixel_format, "Width": width,
                  "Height": height}
        if key not in values:
            raise api.SettingError(f"a webcam has no setting called "
                                   f"{key!r}")
        return values[key]

    def write(self, setting: Any, value: Any, batch: Any) -> None:
        if setting.key != "PixelFormat":
            raise api.SettingError(f"{setting.label} cannot be changed")
        if self.device.streaming:
            raise api.NeedsStop(setting.key)
        self.device.pixel_format = value
