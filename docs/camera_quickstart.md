# Running Barbie Capture / Typhon against a real camera

`barbie_capture_v5` is the Barbie GUI with a live camera driving it and a
setup wizard that runs the first time it opens. `typhon` began as the same app
in `#045f80` and has since gained what the first EVK4 test asked for: frames
saved without slowing the camera, the EVK4's `.raw` recorded alongside them,
and a slider that follows the capture, plays like a video and swaps between
the PNGs and the raw. **Use Typhon**; v5 has none of that. This is what to do
on a machine with a camera.

## What is verified, and what is not

**Basler / pypylon — verified end to end** through pylon's camera emulator
(`PYLON_CAMEMU`): enumeration, opening, the node map, AOI, exposure, gain,
grabbing, Mono12 → uint16, and a lossless 16-bit PNG round trip — on numpy 1.x
and 2.x. The emulator cannot prove the CoaXPress transport itself or the
boA5320's own node set.

**Prophesee / Metavision — verified against a real Metavision build, not a
camera.** OpenEB was built from source and the backend driven through a
synthetic EVT2 recording: callback registration, decoding, event fields and
coordinates, end of stream. The **raw recording** was checked the same way:
the `.raw` Typhon writes is byte-for-byte what the camera sent, it plays back
with every event, each window drawn exactly as the live view drew it, and
PNG ↔ raw lands on the same moment (to the microsecond). Still unproven:
enumerating and opening a live USB EVK4, and a real EVK4's EVT3 stream.

## 1. Get the branch

```bash
git clone https://github.com/Infernoplaystuf/Council.git
cd Council
git checkout qt-migration
```

## 2. Make an environment for the camera

The app runs under whichever Python you point it at, so the camera SDK does not
have to live in the Council's own environment.

```bash
conda create -n camera python=3.11 -y
conda activate camera
pip install PySide6 numpy pillow scikit-learn
```

You do not need to work out the rest yourself: the setup wizard (step 4) tells
you what to install for your camera and checks it afterwards. What follows is
the same information, for reading ahead.

### Basler — CoaXPress needs more than the pip wheel

A **boA5320-150cm** is boost series on CXP-12. `pip install pypylon` alone can
never find it: measured here, the pylon runtime inside the pip wheel offers
**USB and GigE transport layers only — no CoaXPress**.

1. Install the **pylon Camera Software Suite** from Basler
   (<https://www.baslerweb.com/en/downloads/software/>) and, when it asks which
   camera interfaces to install, include **CoaXPress (CXP)**.
2. `pip install pypylon` into the camera environment.
3. In the pylon Viewer, confirm the camera appears and that the interface
   card's applet matches the number of CXP cables in use.
4. **Close the pylon Viewer** — a frame grabber can only be held by one program.

### Prophesee EVK4 — the SDK, the Python, and a USB driver

1. **The Metavision SDK** is not on pip. Two routes
   (<https://docs.prophesee.ai/stable/installation/index.html>):
   - the **official Windows installer** — installs the camera's USB driver for
     you, but downloading it needs an account requested from Prophesee;
   - **OpenEB**, the open-source core — no account, but a from-source build with
     Visual Studio 2022 that takes a few hours.
2. **Python 3.10, 3.11 or 3.12.** The bindings will not import under any other.
3. **The USB driver — if you built OpenEB.** Windows has no driver for an EVK4,
   and without one the camera is simply *not found*, exactly as if it were
   unplugged. Download `wdi-simple.exe` from the "Camera Plugins" section of
   <https://docs.prophesee.ai/stable/installation/windows_openeb.html>, then in
   a Command Prompt opened **as administrator**:

   ```bash
   wdi-simple.exe -n "EVK" -m "Prophesee" -v 0x04b4 -p 0x00f4
   ```

   ```bash
   wdi-simple.exe -n "EVK" -m "Prophesee" -v 0x04b4 -p 0x00f5
   ```

   ```bash
   wdi-simple.exe -n "EVK" -m "Prophesee" -v 0x04b4 -p 0x00f3
   ```

4. Plug the EVK4 into a **USB 3** port directly, not through a hub.

## 3. Build the app for Qt

```bash
python run_example_gui.py barbie_capture_v5 --target qt --python camera --no-run
```

or, for Typhon:

```bash
python run_example_gui.py typhon --target qt --python camera --no-run
```

`--target qt` is required — the live view uses the Qt image canvas. `--python
camera` records that environment in the project, so the GUI Designer's **Run**
uses it too. The command prints the project directory and the line to run it.

There is **no longer an `app.py` line to add by hand**: a wireframe that links
buttons to `frame_camera` is generated with `frame_camera.attach(self)` already
written in. (A project built from v4 before this change still needs the line;
adding it to a new one as well is harmless — the second call does nothing.)

## 4. Run it — the setup wizard

```bash
python "<project>/main.py"
```

The first time, a **Camera setup** wizard opens over the window:

1. **Which camera?** — Basler or Prophesee EVK4.
2. **What to install** — the steps for that camera, with links, and the exact
   install command for the Python this app is running under. **Copy install
   command** puts it on the clipboard.
3. **Is it installed?** — asks the installed software what it can actually do:
   `✓ Done`, `⚠ Check` (worth attention, may not apply to your camera), or
   `✗ Missing`, each with the step that fixes it. **Check again** after
   installing.

**Finish** is never blocked — finish before the camera is even unpacked if you
like. The choice is saved beside `app.py` as `camera_setup.json`; cancelling
saves nothing, so it is offered again next launch. **Camera setup…** (next to
the "Camera" heading) reruns it at any time.

Once set up, **Scan for cameras** searches only that camera's SDK (plus the two
simulated cameras, which are always there for trying things out), so the
"Backends not searched" panel no longer talks about an SDK you will never use.

No camera yet? Set `PYLON_CAMEMU=2` before starting the app and pylon presents
two emulated Basler cameras — the whole workflow can be tried without hardware.

## Using it

1. Pick a **capture folder** with the folder picker at the top left. **Make it
   a folder on this computer**, not a network drive — see below.
2. **Scan for cameras**, select one, **Connect**. If the folder has no frames
   in it yet, the picture now shows what the camera sees — the **live
   preview** — so you can aim, focus and set the area first. **Nothing is
   saved** during the preview; the line above the picture says
   `Preview · not saving`.
3. **Start capture** — frames are written to that folder (and, for an EVK4,
   the `.raw`).
4. **Stop capture** when done, then copy the run wherever it needs to go.

### The live preview

The preview runs while a camera is connected, nothing is being captured, and
the folder has no frames to look at. Choose a folder that already has frames
and the picture shows those instead (the camera goes quiet, so it does not
slow playback down); choose an empty folder again and the preview comes back.
**Stop capture** during the preview does nothing — there is nothing to stop.
Exposure and gain are applied when you press **Start capture**.

**An EVK4 streams from the moment the preview first starts until you press
Disconnect.** Its stream is never stopped and restarted in between: measured
against Prophesee's SDK, a restarted EVK4 stream can come back with its
clock running backwards or 16.8 s ahead, with false events, or not start at
all — and the Python SDK has no way to reset it. So Start and Stop open and
close the `.raw` inside the running stream, exactly as Prophesee's own
recorder does, and a folder with frames only hides the preview. Two things
follow:

- a capture started **from the preview** can lack up to about 4 ms of events
  at the very start of its `.raw` (the SDK starts a file at its next time
  marker); a capture started straight after **Connect** lacks nothing;
- if an EVK4 stops sending (unplugged, or it fails), press **Disconnect** and
  **Connect** again — a fresh connection is the only clean restart.

A Basler camera has none of this: it is stopped and started freely.

### Save locally, copy afterwards

A network share cannot keep up with a camera. The first EVK4 test saved to a
NAS and the capture crawled. Frames are now saved on their own thread, so a
slow disk no longer slows the camera — but frames the disk cannot take in time
are **not saved**, and the status line counts them (`12 NOT saved (storage too
slow)`). While capturing into a network share, the status line says so
(`NETWORK FOLDER: save locally, copy after`).

PNGs are written by several writers at once, with fast (still lossless)
compression, sized to the machine: one writer per core up to 8 (leaving two
cores for the camera and the window), and a queue of up to a quarter of the
RAM (between 512 MB and 8 GB) to ride out bursts. Measured on EVK4-sized
frames, one writer managed as few as 14 a second in a busy scene; four
writers at the fast setting manage over 200 — the EVK4 makes 50. They finish
out of order but are numbered, listed and indexed in the order the camera
took them.

**If frames are still "NOT saved", lower the frame rate** (below). A
full-frame boA5320 frame is about 49 MB; no disk saves 150 of those a second
as PNG, and capping the camera at a rate the disk can take gives a complete
capture instead of one with gaps.

### Exposure, gain and frame rate

Set these before **Start capture**; they are applied when it starts. **0 means
"leave it as the camera has it"** (a spin box always has a number in it):

- **Exposure µs** — up to 10 s. Basler only; an event camera has no exposure.
- **Gain dB** — up to 48. Basler only.
- **Frame rate fps** — how many pictures a second. On a **Basler** it caps
  the camera (0 = as fast as it goes). On an **EVK4** it sets how long each
  picture collects events: 50 fps = 20 ms windows (the default), 200 fps =
  5 ms, up to 1000 fps = 1 ms. The `.raw` has every event whatever this is,
  and the raw view uses the same window as the run's PNGs.

### Pop out

**Pop out** copies the picture on screen — a saved PNG, a raw window, or the
camera live — into a window of its own. Move it to another monitor and press
**Full screen** (or F11; Esc leaves full screen). The main window carries on
capturing or playing meanwhile. Each press opens another window with its own
copy; scroll to zoom, drag to pan, **Fit** to see all of it.

So: capture into a local folder, press **Stop**, then copy the whole run to
the NAS. Everything is closed at Stop, so nothing is locked while you copy.

### What a run writes

Each run is named after the time it started, and everything it writes sits
side by side in the capture folder:

| File | What it is |
|---|---|
| `<run>_frame_000001.png` … | What the camera looked like, one per frame (an event camera: one per 20 ms window). |
| `<run>_frames.csv` | One row per saved PNG: file, frame index, camera timestamp, and for an event camera the event count and where that PNG falls in the `.raw`. |
| `<run>_events.raw` | **EVK4 only.** Every event the sensor sent, in Prophesee's own format — opens in Metavision Studio and Prophesee's tools. |

The `.raw` is the event camera's real data. A PNG is a 20 ms *picture* of it,
and PNGs can be skipped when storage falls behind; the `.raw` loses nothing.
It grows with how much is changing in the scene, so check free space before a
long run. A second run never overwrites the first — two runs started in the
same second get `_2`, `_3` on the name.

### The slider

- **While capturing**, the slider's right-hand end is **live**: the view line
  above the picture says `Live · 240 saved` and the slider grows as frames are
  saved.
- **Drag it back** to look at a frame already saved — the capture carries on
  underneath, and your place stays put while the slider keeps growing. The
  view line says `PNG 12 / 300 · capturing`. Drag to the end to be live again.
- **Play / Pause** plays the frames like a video, about 30 a second, from
  where the slider is; pressing it at the end starts again from the
  beginning. Played during a capture it becomes live if it catches up, which
  it only does with a camera slower than that. An EVK4 makes 50 PNGs a
  second, so drag to the end to go straight to live.
- **PNG / Raw** (EVK4 runs) swaps to the run's `.raw` **at the same moment**
  as the PNG on screen, and back again. The raw view is every event in 20 ms
  windows and plays in real time. It opens straight away and fills in while
  the file is read; the view line says `reading` until it has all of it. It
  opens once a run is stopped (the file is still being written until then).
  Viewing a `.raw` needs the Metavision SDK on that computer.

**Mark this frame** and **Predict this frame** use the PNG on screen; while
live or in the raw view there is no file on screen for them to use.

### Cropping to a region and saving it

Draw a box on the picture with **Draw ROI** (or type `x, y, w, h` into the ROI
box), choose **Save cropped frames to**, and press **Save cropped frames**:
every PNG in the capture folder is cropped to that box and saved there, with
the originals untouched. It works on the PNGs — during a capture, after it, on
an EVK4 run as well as a Basler one. It does not crop the `.raw`.

### The area of interest (Basler)

Type `x, y, w, h` into the ROI box, or drag a rectangle on the live view, then
**Apply area to camera**. This sets the camera's **own AOI**, so frames arrive
at that size and are saved at that size. It is snapped to what the sensor
accepts, and the status line says so when it had to move. **Full sensor** puts
it back. (On an EVK4 run, stop the capture first: changing the area restarts
the camera, which would cut the `.raw` short, so Typhon refuses.)

### Reading the status line

`60.0 fps · 242 grabbed · 157 not drawn (screen only) · 242 saved`

- **not drawn (screen only)** counts frames the *screen* did not redraw — the
  display shows about 30 a second, whatever the camera does. Normal and
  deliberate; **every one of them is still saved.** Only **NOT saved** means
  frames missing from disk.
- **saved** is frames on disk. **waiting to save** appears when the disk is
  behind; **NOT saved (storage too slow)** counts frames it could not take at
  all. If a write *fails* (disk full, folder gone), recording stops and says so.
- **Stop** gives the disk a second to catch up and then hands the window back.
  Anything still waiting keeps saving and the status line counts it down
  (`Stopped — still saving`); closing the app waits for it.

An event camera reports events per window and the event rate instead of frames
per second: it has no frames.

Each run writes under its own timestamped stem, so a second run into the same
folder can never overwrite the first. Unplugging the camera mid-capture ends
the capture with one clear message rather than an endless stream of errors.

## Pixel depth

A Mono12 frame arrives as `uint16` holding 0–4095 and is saved as a 16-bit PNG
with those values **unaltered** (verified lossless). One known consequence:
`frame_classes.thumbnail` scales 16-bit input as if it filled the full range,
so a 12-bit frame reads dark *to the classifier*.

## If something goes wrong

| What you see | What it means |
|---|---|
| Basler: empty camera list | Rerun **Camera setup…** and read the Check page. For a boA5320 it is almost always missing CoaXPress support. |
| "could not open … something else is holding the frame grabber" | The pylon Viewer, or a previous run, still owns the card. Close it. |
| EVK4: empty list with the camera plugged in | The USB driver, almost always (Prophesee step 3). The SDK cannot tell "no driver" from "no camera" — both are an empty list. Other causes that look identical: Metavision Studio or another program has the camera open (close it); on an OpenEB build, `MV_HAL_PLUGIN_PATH` not set or `libusb-1.0.dll` not on `PATH` (run the build's `utils\scripts\setup_env.bat` first); camera firmware older than 3.8. |
| EVK4: finding out *why* it was skipped | Start the app with `MV_LOG_LEVEL=TRACE` set and read the console — the SDK only explains a skipped camera there. |
| EVK4: connected, but hardly any events | Expected with a still camera on a still scene: an event camera only reports *change*. Wave a hand in front of it. |
| EVK4: "Metavision SDK installed" fails | Not installed, or installed for a different Python than the app runs under (3.10–3.12 only). |
| Camera connects, live view stays blank | Check the AOI — **Full sensor** resets it. |
| `NOT saved (storage too slow)` in the status line | The folder is on a disk (usually a network share) that cannot keep up. Capture to a local folder and copy the run afterwards. |
| **PNG / Raw** says `Raw opens after Stop` | The `.raw` is still being recorded. Stop the capture first. |
| **PNG / Raw** says `No raw file for this run` | A Basler run (only event cameras record a `.raw`), or the `.raw` was not copied along with the PNGs. |
| **PNG / Raw** says the raw view needs the Metavision SDK | Viewing a `.raw` uses Prophesee's SDK; install it on this computer (Camera setup…, EVK4). |
| "stop the capture before changing the camera's area" | EVK4 only — see *The area of interest*. |

For an EVK4 the **official installer is the lower-risk route**: it installs the
USB driver and registers where its plugins live, which removes three of the
silent causes above. OpenEB works, but those three become your job.
