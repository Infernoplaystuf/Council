# Adding a camera to Typhon: the plugin guide

Typhon finds, opens and records cameras through **camera plugins**. Basler
(USB3, GigE, CoaXPress), Prophesee EVK4 and the simulated cameras are
plugins that ship with Typhon. You can add support for any other camera by
writing a plugin for it: one folder holding a small `plugin.json` and some
Python. You don't need to change any of Typhon's own code.

This guide covers plugin API **1.0**. It walks through a complete working
example, a plugin for any USB webcam that OpenCV can open:
[`examples/typhon_plugins/example_webcam/`](../examples/typhon_plugins/example_webcam/).

> **Where plugins run.** Plugins run in projects made from the **Typhon
> (plugins)** example (`typhon_plugins` in the Designer's "New from example").
> The older classic `typhon` example doesn't load plugins.

---

## 1. The short version

1. **Copy** `examples/typhon_plugins/example_webcam/` and give it a new
   name, for example `acme_thermal/`.
2. **Edit `plugin.json`.** Set a new `id` (`acme.thermal`), `short`
   (`therm`), `name`, `author`, `description` and `licence`.
3. **Edit `plugin.py`.** Replace the OpenCV calls with your camera SDK's
   calls. Set `BACKEND` to your id (`"acme.thermal"`).
4. **Check it** (no camera needed for most checks) from a Council checkout:

   ```
   python -m typhon_plugin_api.check path\to\acme_thermal
   ```

5. **Install it.** In Typhon (plugins), open **Plugins…**, press **Open
   plugins folder**, drag your folder in, and press **Rescan**.
6. **Approve it.** Press **Review and approve…** and read what it shows,
   then press **Approve**. That turns the plugin on in the open project.
   Your cameras appear the next time you press **Connect**.

You approve each plugin once. If you later drag a new version over it, the
new version runs at the next start without asking again.

---

## 2. The folder

```
acme_thermal/              any folder name works; Typhon goes by the id
  plugin.json              the manifest: what the plugin is
  plugin.py                the code the manifest's "entry" names
  helper.py                more modules if you want (relative imports)
  data/lut.json            data files, read with ctx.read_data()
  tests/test_thermal.py    your own tests (optional, recommended)
```

Typhon refuses a folder that holds compiled code (`__pycache__`, `.pyc`,
`.pyd`, `.dll`, `.so`) or more than 2,000 files. Ship source only. Native
code comes from the SDK you install into Python yourself, such as pypylon or
OpenCV.

---

## 3. `plugin.json`

The example's manifest:

```json
{
  "schema": 1,
  "id": "example.webcam",
  "short": "webcam",
  "name": "USB webcams (example)",
  "version": "1.0.0",
  "api": "1.0",
  "kind": "camera",
  "entry": "plugin:WebcamPlugin",
  "author": "The Council examples",
  "description": "Any webcam OpenCV can open, as a Typhon camera.",
  "licence": "MIT",
  "network": "none",
  "capabilities": ["frames", "restartable"]
}
```

| Field | Required | What to put |
|---|---|---|
| `schema` | yes | `1` |
| `id` | yes | Reverse-domain, lower-case letters and digits between dots: `acme.thermal`. **No underscores and no hyphens.** `typhon.` is reserved for built-in plugins. |
| `short` | yes | 2 to 12 lower-case letters or digits, unique. It's used in the names of any run files the plugin writes. |
| `name` | yes | Shown on screen. |
| `version` | yes | `major.minor.patch` |
| `api` | yes | `"1.0"`: the lowest plugin API the plugin needs. A plugin for 1.x runs on any Typhon with API 1.x where x is the same or later. |
| `kind` | yes | `"camera"` for a camera plugin. (`"feature"` and `"source"` are the other kinds; see section 11.) |
| `entry` | yes | `module:object` inside the folder. Typhon calls the object with no arguments to make the plugin. |
| `author`, `description`, `licence` | yes, for your plugin | Shown in the review before approval. |
| `capabilities` | camera plugins | What your cameras **can** have (section 7). Each opened camera confirms its own. It may never confirm one that isn't listed here. |
| `network` | no | `"none"` (the default), `"camera-link"` (a camera on this PC's local network, such as GigE), or `"internet"` (refused unless the user ticks **Allow internet** for the plugin). |
| `python` | no | Python packages the plugin can't load without, as **import names** with optional bounds: `["numpy>=1.24", "cv2"]`. A missing one turns the plugin off and the Plugins window says what to install. Typhon never installs anything. For a camera SDK, it's usually better to leave it out and have `available()` say what to install (the example does this). |
| `contributes` | no | For a camera plugin: `"setup"` puts your camera family in the set-up wizard (section 8); `"review_source"` gives the review slider a raw file of the camera's own; `"presets"` keeps values of your own in each camera's presets. |
| `files` | no | Run-file endings your plugin writes: `_<short>_<what>.<ext>`, for example `_therm_temps.csv`. |

Typhon rejects a manifest with unknown fields, duplicate keys, NaN, or text
that isn't UTF-8, and the error names the field.

---

## 4. The plugin object (`CameraPlugin`)

This is the object the manifest's `entry` names. Typhon makes one per
window.

```python
import typhon_plugin_api as api

class AcmePlugin:
    backend = "acme.thermal"            # your cameras' family: WITH A DOT

    def activate(self, ctx): self.ctx = ctx
    def deactivate(self): self.ctx = None

    def available(self):                # -> (ready?, what to install)
    def discover(self):                 # -> [api.CameraInfo, ...]
    def open(self, info):               # -> your api.Device subclass
    def capabilities(self, device):     # -> frozenset of capability names
    def facet(self, device, name):      # -> a facet, or None
    def categories(self):               # -> api.CategoryHints
    def record_fields(self, device):    # -> plain JSON for the run record
    def review_source(self):            # -> api.ReviewSource or None
```

| Method | What Typhon expects |
|---|---|
| `backend` | Your cameras' `CameraInfo.backend`. Use your id. A plugin that isn't built in **must** use a name with a dot, so it can never take over a built-in's presets. |
| `available()` | `(True, "")`, or `(False, "what to install")`. **Never raise** for a missing SDK: import it inside a function, not at the top of the file. The camera scan shows your hint. |
| `discover()` | Every camera of yours this PC can see, as `api.CameraInfo(backend, key, model, serial, vendor, kind)`, **without keeping any open**. `key` must be stable and unique; `kind` is `"frame"` (or `"event"` for an event camera). |
| `open(info)` | Open that camera and return your `api.Device`. If the camera refuses (in use, unplugged), raise `api.CameraError("why, in plain words")`. The user sees that as the camera's answer, not as your plugin failing. |
| `capabilities(device)` | Usually `frozenset(api.device_capabilities(device))`: Typhon reads the basics off your device's flags. Add trigger or binning names only when this camera really has them (section 7). |
| `facet(device, name)` | `None` unless you support triggers or binning (section 7). |
| `categories()` | Which settings tab each settings group goes in. `api.CategoryHints()` with no arguments gives each group a tab of its own. |
| `record_fields(device)` | A small dict of plain JSON (numbers, text, lists, dicts) saved into each run's camera record: firmware, sensor temperature, and so on. |
| `review_source()` | `None`, unless your camera writes its own raw file that the review slider should show (the Prophesee plugin's `.raw`). |

**Threads and time limits.** `activate`, `deactivate` and `discover` run on
the UI thread, so keep them quick (`discover` may take a second or two).
`record_fields` runs at Start, and Typhon stops waiting for it after 2 s.
A call that hangs for more than 2 s is switched off by Typhon's watchdog.

**Failures.** If your plugin raises anything other than `CameraError`, it's
logged to `<project>/plugin_errors.log`, the status line says so, and a
plugin you added is switched off for the rest of the session. A failing
plugin never stops a recording that's already running.

---

## 5. The camera (`Device`)

Subclass `api.Device`. These are what you **must** write:

```python
class AcmeDevice(api.Device):
    restartable = True       # stop() then start() on one connection is fine
    area_live = False        # set_roi only while stopped

    def __init__(self, info, sdk):
        self.info = info                   # required
        ...open the camera...

    def limits(self):        # -> api.Limits(width=, height=, inc_x=, ...)
    def roi(self):           # -> api.Roi(x, y, w, h), the area in use now
    def set_roi(self, roi):  # -> the api.Roi actually set
    def start(self):         # begin streaming; set self._started = True
    def stop(self):          # end streaming; set self._started = False
    def read(self, timeout_ms=1000):   # -> api.Frame or None
    def close(self):         # release the camera
```

The rules that matter:

- **`read(timeout_ms)`** is called only from Typhon's grab thread. Return
  the next frame, or `None` if none arrived within the timeout. It must
  return **within the timeout plus 100 ms**, even when the camera has
  stopped sending.
- **A frame owns its array.** Give `api.Frame(image=..., index=...,
  timestamp_us=..., meta={...})` an `image` that is a numpy array you own
  outright: a **copy**, never a view into an SDK buffer that the SDK will
  reuse. Use `uint8` or `uint16`, with shape `(h, w)` for grey or `(h, w, 3)`
  for colour. `index` counts up from 1. `timestamp_us` is the camera's own
  clock if it has one (say in `record_fields` what unit it is in).
  `meta["aoi"]` is the area `(x, y, w, h)` the frame was taken with, and
  `meta["format"]` is the pixel format name (`"Mono8"`, `"Mono12"`).
- **The area.** `set_roi` gets an `api.Roi` in sensor pixels and returns
  the area the camera actually took, after snapping it to the camera's
  increments. An empty `Roi` means the full sensor. If the camera can't
  change its area while streaming, raise `api.NeedsStop("roi")` **before
  writing anything**. Typhon then stops the stream, retries and restarts.
- **`streaming`** comes from `self._started`. Set it in `start` and `stop`.
- **Optional:** `set_exposure_us`, `set_gain`, `set_frame_rate` (each
  returns the value in effect), `prepare(recording)`, and
  `settings_provider()` (section 6).

When a grab fails but the stream carries on, raise `api.GrabFailed(why,
index)` so the frame number is used up and the run's counts stay true.
When the camera is gone for good (unplugged), raise
`api.CameraError`.

---

## 6. Settings (`Provider` and `Setting`)

Typhon's settings tabs are built from what the camera says about itself.
Return a `Provider` from `settings_provider()`:

```python
class AcmeSettings(api.Provider):
    def describe(self):     # -> [api.Setting, ...], with current values
    def read(self, key):    # -> the camera's value now
    def write(self, setting, value, batch):   # write it, or raise
```

A setting:

```python
api.Setting(key="ExposureTime", label="Exposure", group="Exposure",
            kind="float", value=5000.0, minimum=20.0, maximum=1e6,
            unit="µs", live=True, help="How long each picture collects light.")
```

- `kind` is one of `"float"`, `"int"`, `"bool"`, `"choice"` (give
  `choices=(...)`), or `"text"` (shown, not saved in presets).
- `read_only=True` means the value is never writable (a temperature).
  `live=False` means it's writable only while stopped: raise
  `api.NeedsStop(key)` from `write` while streaming.
- Typhon's own `set()` does the rest: it fits the value to the range,
  writes it, reads it back, says what the camera actually took, and saves
  it in presets.
- `group` names the settings tab. Use `categories()` to put several groups
  on one tab.
- Raise `api.SettingError` for a key you don't have.

If your camera speaks GenICam, read the Basler plugin and
`council_core/camera_settings.py`'s `BaslerSettings` for how nodes, ranges
and trigger selectors become settings.

---

## 7. Capabilities, triggers and binning

`capabilities` in the manifest lists what your cameras can have. Typhon
knows these names:

| Name | Meaning | How it's confirmed |
|---|---|---|
| `frames` / `events` | picture frames, or an event stream | `CameraInfo.kind` |
| `restartable` | stop then start on one connection is safe | `Device.restartable` |
| `area_live` | the area can change while streaming | `Device.area_live` |
| `grab_failures` | failed grabs are counted | `Device.counts_grab_failures` |
| `raw_recording` | the camera records its own raw file | `Device.records_raw` |
| `software_trigger` | Typhon can fire the camera | your `capabilities()` and the `SoftwareTrigger` facet |
| `hardware_trigger` | the camera waits for an input line | your `capabilities()` and the `HardwareTrigger` facet |
| `binning` | the camera averages its own pixels | your `capabilities()` and the `Binning` facet |
| `camera_clock` | frames carry a usable camera time stamp | your `capabilities()` |

`api.device_capabilities(device)` reads the first five off your device's
flags. For the trigger and binning ones, give the device a **capture
set-up** (`device.capture = api.CaptureSetup()`), implement the trigger
methods of `api.Device` (`capture_options`, `trigger_problem`,
`arm_trigger`, `disarm_trigger`, `trigger_ready`, `fire_trigger`, and
`count_edges` for a hardware input), add the name in `capabilities()`, and
return the facet from `facet()`. The Basler plugin does all of this:
`typhon_plugins/basler/plugin.py` with `council_core/cameras.py`'s
`BaslerDevice`. A camera with no capture set-up still gets Typhon's
Continuous and Snapshot runs, and Downsample by averaging.

Never confirm a capability your manifest doesn't declare. The check (section
9) fails a plugin that does.

---

## 8. The set-up wizard (optional)

A camera plugin whose manifest lists `"setup"` in `contributes` becomes a
check box in the project's camera set-up wizard. It brings install steps,
checks and an optional scan page. A plugin **without** `"setup"` is always
scanned, which is simpler and what the example does. To add one, implement
`setup_label`, `setup_guide(python)` (returns an `api.Guide` of
`api.Step`s) and `setup_checks()` (returns an `api.Readiness` of
`api.Check`s), as the Basler plugin does. Once a project's set-up has been
run, a plugin with `"setup"` is scanned only if its box is ticked.

---

## 9. Testing your plugin

**The check command.** Run it from a Council checkout, or with one on
`PYTHONPATH`:

```
python -m typhon_plugin_api.check path\to\acme_thermal
```

It reads your folder the way Typhon will (manifest rules, no compiled code,
imports only through `typhon_plugin_api`), loads it into the **fake host**
(a stand-in Typhon with a temporary project and vault, no window), and then:

- checks `available()`, `discover()`, and for the first camera found: open,
  area, settings, start, read (timing, dtype, a frame that owns its array),
  stop, close (twice), capabilities against your manifest, categories,
  record fields and review source;
- has **Typhon itself** connect that camera and record 3 frames, then checks
  that the saved pictures, the frames CSV and the end record all agree;
- runs your folder's own `tests/` with pytest.

With no camera connected, the open and record checks are skipped and the
output says so. If your SDK isn't installed, the camera checks are skipped
and the output shows your install hint. The command exits 0 when nothing
failed.

**Your own tests, with no camera.** Give the plugin object an attribute for
its SDK (the example's `cv2 = None`), and in tests put a small stand-in
there. `examples/typhon_plugins/example_webcam/tests/test_webcam.py` does
this with a `FakeCv2` and then drives the plugin through Typhon:

```python
from typhon_plugin_api.fake_host import FakeHost

def test_typhon_captures_from_it(tmp_path):
    with FakeHost(tmp_path) as fake:
        fake.load(PLUGIN_FOLDER)
        fake.instance("acme.thermal").sdk = FakeSdk()
        fake.connect("frame", label="Acme")      # as the Connect button does
        run = fake.capture(frames=3)             # Start, 3 frames, Stop
        assert run.problems() == []
```

---

## 10. Installing, approving, updating

**Where plugins go.** Plugins go in the `plugins` folder of Typhon's plugin
home. **Plugins… → Open plugins folder** opens it. The default is
`~/.council/typhon/plugins/`. When Typhon (plugins) runs beside today's
Typhon through `run-typhon-plugins.bat`, it's
`~\Typhon-plugins-app\typhon\plugins\`. Each plugin is its own folder, and
the folder can have any name.

**What Typhon reads, and when.** When Typhon starts (or opens a project, or
you press **Rescan**), it reads every file of every plugin folder and
records each file's sha256. **Those exact bytes are what run.** Editing a
file while Typhon is running changes nothing until the next start, and a
data file changed mid-session is refused by `ctx.read_data()`. Typhon never
writes bytecode into your folder.

**Approval.** A new plugin shows **needs approval**. **Review and
approve…** shows its manifest, the packages it needs and whether this
Python has them, every file with its size and sha256, and a scan for lines
worth a look (subprocess, file deletion, network, ctypes...). Approval is
kept by the plugin's **id** in the plugin home's `approvals.json` and is
never asked for again. **Withdraw approval** takes it back.

> An approved plugin is ordinary Python running with Typhon's rights. The
> review and the network tripwire catch mistakes; they don't fence in a
> plugin written to do harm. Approve only plugins whose author you trust.

**Network.** A plugin's code is watched for socket connections. `none`
allows only this PC, `camera-link` also allows private and link-local
addresses (GigE cameras), and `internet` allows anything once the user has
ticked **Allow internet**. A refused connection raises `PermissionError`
inside your plugin and the status line says so. A camera SDK's own native
traffic isn't affected.

**Updating.** Drag the new version's folder over the old one, or replace its
files. It runs at the next start with no new approval. Only one version of a
plugin is installed at a time. Two folders with the same id are both
refused until you take one out. Each run's camera record names the plugins
that made it, with each one's version and a sha256 of its files.

**Data files.** Read them with `self.ctx.read_data("data/lut.json")`, which
returns the bytes as read at start-up. Don't `open()` your own folder.

---

## 11. Other kinds of plugin

The same folder, manifest, review and approval apply to:

- **feature** plugins: pop-out panels, toolbar buttons, run hooks
  (`check_start`, `on_run_started`, `on_run_finished`), live frames
  (`on_frame`) and datasets. See the built-in Classification
  (`typhon_plugins/classify/`) and Send trigger (`typhon_plugins/send_trigger/`).
- **source** plugins: a trigger box that says "capture now" (`sources()`,
  `make_source(name)`). See `docs/handoff/typhon_triggering_2026-11-02.md`.

The full API reference is `typhon_plugin_api/__init__.py`, and its working
notes are `docs/typhon_plugin_api_notes.md`.

---

## 12. House rules

- Import Typhon only through `typhon_plugin_api` (plus the standard library,
  numpy and your SDK). Only a panel module, never the entry module, may
  import PySide6's `QtCore`, `QtGui` and `QtWidgets`.
- Offline: nothing leaves the PC unless the user opts in. No telemetry.
- Nothing downloaded or installed. No pickle. No `QSettings`.
- Never write over or delete user data. Write run files only through
  `ctx.create_file` and `ctx.write_new`, and your own files only into
  `ctx.data_dir()`.
- Ask `api.has("feature.name")` for optional features instead of comparing
  version numbers.

---

## 13. When something is wrong

| What the Plugins window says | What to do |
|---|---|
| *needs approval* | Press **Review and approve…**. |
| *it asks to use the internet...* | Tick **Allow internet** in the review, or remove `"internet"` from the manifest. |
| *its id ... holds an underscore* | Use dots and plain letters: `acme.thermal`, not `acme_thermal`. |
| *another folder ... holds a plugin with the id ...* | Two copies are in the plugins folder; take one out. |
| *it holds compiled code* | Delete `__pycache__`, `.pyc` and `.dll` files from the folder. |
| *it was written for plugin API X* | Set `"api"` to the version this Typhon has (`1.0`). |
| *needs Python package ...* | Install it into the Python that Typhon runs under. Typhon never installs anything. |
| *faulted* | Look in `<project>/plugin_errors.log` for the traceback. The plugin is off until the next start. |
| *its backend name ... has no dot* | Set `backend` to your id. |
| The plugin is on but no camera appears | Press **Connect**: the scan notes say what's missing (`available()`'s hint). If your manifest contributes `"setup"`, tick your family in the camera set-up wizard. |

---

## Keeping this guide current

This guide must match the code. **Any change to Typhon's camera or plugin
code updates this guide and the webcam example in the same change.** That
covers `typhon_plugin_api/`, `typhon_plugins/`,
`council_core/plugin_host.py`, `plugin_loader.py`, `plugin_manifest.py`,
`plugin_lists.py`, `cameras.py` (`Device`, `Frame`, `CameraInfo`),
`camera_settings.py` (`Provider`, `Setting`), `camera_capabilities.py`,
`council_qt/widgets/plugin_panels.py`, the fake host and the check command.
`tests/test_typhon_plugins_check.py` runs the example through the check, so a
change that breaks the example fails the suite.

Plugin API history: 0.1 to 0.3 were working versions while the built-in
plugins shaped it (October 2026). **1.0** (9 November 2026) froze the API.
From 1.0, an addition raises the minor number and a removal or change raises
the major.
