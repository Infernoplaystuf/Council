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
and 2.x. Every pixel format the emulator offers was grabbed and saved; colour
order was checked against a known red picture served by the emulator (RGB8,
BGR8 and BGRA8 all come out red-first). The **Basler scan** runs end to end on
it. The emulator cannot prove the CoaXPress transport itself, the boA5320's
own node set, or a card: the scan's **CoaXPress card, link and power checks
come from Basler's documentation and are untested** until a card is fitted.

**Prophesee / Metavision — verified against a real Metavision build, not a
camera.** OpenEB was built from source and the backend driven through a
synthetic EVT2 recording: callback registration, decoding, event fields and
coordinates, end of stream. The **raw recording** was checked the same way:
the `.raw` Typhon writes is byte-for-byte what the camera sent, it plays back
with every event, each window drawn exactly as the live view drew it, and
PNG ↔ raw lands on the same moment (to the microsecond). Still unproven:
enumerating and opening a live USB EVK4, and a real EVK4's EVT3 stream.

**Camera settings and presets.** Basler: verified on the emulator (ranges
from its nodes, clamping, the camera's own snapping, what is locked while
grabbing, a preset applied back with its area). EVK4: every facility name,
method, enum member and range accessor used was checked against the OpenEB
bindings, and a `.raw` opened as a device lists only what it has — but a
real EVK4's biases, ERC, anti-flicker and trail filter have **not** been
written to a sensor yet: the first real test is the first run. The
**settings window and the preset picker** were driven offscreen in a
generated Typhon, in two separate processes (save, quit, start again, pick):
on both simulated cameras, and on the emulator with a real `BaslerDevice`
(a Gain, the pixel format and the camera's area saved and put back).

**An event camera's identity** (its sensor, serial and integrator, read on
open through `I_HW_Identification`) is checked against the OpenEB bindings
and a real file-backed device; a live EVK4's answer is the first real test.
**The run's camera record** and **the raw view of a run's area** were
checked on the simulated cameras and, for the raw view, on a real OpenEB
recording of events inside an area.

**The classifier library** was driven offscreen through a built Typhon's own
buttons and dropdown, in a temporary vault: a model made, frames marked
through the slider, trained, a folder classified (`Classified with …`),
exported, deleted, imported back under its name; a Barbie project's model
listed with where it came from, the filter narrowing to each app, and
Typhon refused to train Barbie's model but allowed to copy it.

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

Inside the Council instead: **GUI Designer → New from example…**, pick
`typhon` (Qt is already chosen), type `camera` under **Run with**, and
**Build**. The project opens on the canvas when it is built.

There is **no longer an `app.py` line to add by hand**: a wireframe that links
buttons to `frame_camera` is generated with `frame_camera.attach(self)` already
written in. (A project built from v4 before this change still needs the line;
adding it to a new one as well is harmless — the second call does nothing.)

### Already have a Typhon? Update it from inside the Council

A project built from an example keeps the example as it was **that day**. A
Typhon built before the FPS box still says **Frame count**, and has no
**Settings** button; one built before the presets has no preset picker,
**Save preset** or **Camera settings…** button; one built before the
settings tabs has the exposure, gain and FPS boxes loose under the image
folder and no tabs. Updating moves those three boxes into the **Basic** tab
with their names, ports and links, so your own code in their handlers and
in Start is kept and keeps working (checked on the Typhon example as it
was just before the tabs, with the FPS and Start handlers edited). To bring it up to date
without losing your own code:

1. **GUI Designer → Open**, pick your Typhon (`example_typhon` if you used the
   command above).
2. **Update from example…**. A project that does not record which example it
   came from asks — `typhon` is offered first when the name says so.
3. Read the confirmation and press **Yes**. It says what happens:
   - the **drawn layout** — every shape from the example, the window
     settings, the canvas size and the packages it needs — is replaced with
     the example's current one. Shapes **you drew yourself** in the Designer
     are kept on top of it (the log lists them under `kept, drawn by you`), so
     a handler you wrote for one of them still has its button — but an
     example shape you moved or relabelled goes back to the example's;
   - **`handlers.py` and `app.py` are kept.** Generate then rewrites only the
     handler stubs nobody has edited (Start gains `frame_rate`, the FPS box and
     Settings get theirs), adds stubs for new widgets, and names — with
     `handlers.py:<line>` — any handler **you edited** whose link changed, so
     you can bring it in line yourself;
   - the old `project.gspec` is copied to `project.gspec.<time>.bak` in the
     project folder first. Copy it back over `project.gspec` to undo.
4. The log says what changed in a few lines (shapes added, rewired, relabelled;
   the port `frame_count` renamed to `frame_rate`) and ends `policy: OK`.
   **Run** starts the updated app. Unsaved edits on the canvas are saved first,
   so the backup holds them.

The same from a terminal, deleting nothing (unlike `--force`):

```bash
python run_example_gui.py typhon --target qt --update --no-run
```

`--project NAME` updates a project with another name. Measured on the old
"Frame count" Typhon: the update and its Generate take about 0.3–0.5 s.

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

For a Basler, the same page also runs **Scan Basler cameras** — see below.

**Finish** is never blocked — finish before the camera is even unpacked if you
like. The choice is saved beside `app.py` as `camera_setup.json`; cancelling
saves nothing, so it is offered again next launch. **Camera setup…** (next to
the "Camera" heading) reruns it at any time.

Once set up, **Scan for cameras** searches only that camera's SDK (plus the two
simulated cameras, which are always there for trying things out), so the
"Backends not searched" panel no longer talks about an SDK you will never use.

No camera yet? Set `PYLON_CAMEMU=2` before starting the app and pylon presents
two emulated Basler cameras — the whole workflow can be tried without hardware.

### Scan Basler cameras

Runs by itself when the wizard reaches its check page with Basler chosen, and
again from its button. Every Basler camera this PC can see gets a verdict —
**✓ Ready to capture**, **⚠ Works, with limits** or **✗ Will not work yet** —
and every check under it says what it found and, for a problem, *what to do*.
It takes about a second plus a fraction of a second per camera, off the
window's thread.

**The PC first** (top of the report):

- which pylon interfaces exist — no CoaXPress means a boA5320 cannot appear;
  with a CoaXPress card fitted that is a failure, not a warning
- a Basler USB camera or CXP card that **Windows has a driver problem with** —
  pylon cannot see those at all, so without this check the scan could only say
  "none found"
- the CXP card's **PCIe slot**: a slot with fewer lanes (or an older
  generation) than the card returns incomplete full frames at speed
- camera software **installed after the app started** — close the app (and
  the terminal it came from) and start it again
- the card itself: its applet, over-current on a port, cameras it is only
  *simulating*, and whether its 6-pin power is connected

**Then each camera**, found without opening it: interface, family, and
whether it can work at all — **Camera Link** (pylon only configures it; the
images come through the grabber maker's software), **3D** cameras, a GigE
camera on **another subnet** (found anyway: pylon's normal listing hides
those), one **in use** by another program, or one already **connected in
Typhon** (reported, never opened twice).

**Then opened as it is** — pylon's usual clean-up is held back so the scan
sees what the camera will really start with: trigger mode on any selector,
acquisition mode, exposure mode and auto exposure, the frame-rate limit and
what the camera expects to reach, exposure longer than a second, the pixel
format (and every format it offers, judged by what Typhon actually saves),
link speed — USB speed, **the CoaXPress link against the one the camera is made
for** (a boA5320-150 reaches 149.81 fps full frame on 2 links, 75.71 on 1 link
with its own power, 38.09 on 1 link powered over CXP), throughput limits, the
GigE driver and packet size — test pattern, binning, a centred area, the
startup user set, the card's pixel format and applet, temperature.

**Then a test grab**, run the way Typhon runs the camera, for at most about two
seconds: frames arrive, none fail, no buffers lost. A camera limited to under
one frame every two seconds is not test-grabbed; it is reported instead, since
the live view will be that slow too. The camera is **released** afterwards —
Typhon can connect straight away.

## Using it

1. Pick a **capture folder** with the folder picker at the top left. **Make it
   a folder on this computer**, not a network drive — see below.
2. **Scan for cameras**, select one, **Connect**. The picture now shows what
   the camera sees — the **live view** — so you can aim, focus and set the
   camera's area first. **Nothing is saved** while it is only live; the line
   above the picture says `Live · not saving`.
3. Optionally set the camera up: the **tabs under the image folder** hold
   its settings by category (each can **Pop out** into a window of its own),
   **Camera settings…** has every setting in one window, a box drawn on the
   picture + **Apply area to camera** sets its own ROI — or simply **pick a
   preset** in the box under the status line or the **Presets** tab. See
   *The settings tabs under the image folder*, *Camera settings and presets*
   and *The bird bath* below.
4. **Start capture** — frames are written to that folder (and, for an EVK4,
   the `.raw`).
5. **Stop capture** when done — the picture goes back to live — then copy the
   run wherever it needs to go.

### The live view

Whenever a camera is connected and nothing is being captured, the picture is
live — **whatever the folder holds**. The frames already in the folder are
still there behind it: the slider's **right-hand end is live**, and dragging
it back shows a saved frame while the camera keeps streaming, so dragging to
the end again is instant (see *The slider*). **Start capture** records;
**Stop capture** comes back to live once the run's last frames are on disk,
and for ten seconds the live line still says how the run went
(`… · last run: 240 saved, raw 31.4 MB`). Only the **raw view** turns the
live view off — the camera goes quiet so it does not slow the raw reader
down — and coming back to PNG brings it back. **Stop capture** while only
live does nothing — there is nothing to stop. Exposure and gain are applied
when you press **Start capture**; the **FPS** box applies the moment you
change it (below).

The live picture costs the window little, measured on a boA5320-size frame
(5328 x 3040, offscreen): a Mono8 frame takes **1.5 ms** of the window's
time to show (it was 10.7 ms — the canvas copied every frame, needlessly),
and a Mono12 frame **9.7 ms** (it was 19.8 ms, *and* the picture was wrong:
the 16-bit frame was drawn as its raw bytes). With the real 33 ms timer and a
30 fps camera of that size, a frame reaches the screen in 19 ms (Mono8,
median; 25 ms Mono12).

**An EVK4 streams from the moment the live view first starts until you press
Disconnect.** Its stream is never stopped and restarted in between: measured
against Prophesee's SDK, a restarted EVK4 stream can come back with its
clock running backwards or 16.8 s ahead, with false events, or not start at
all — and the Python SDK has no way to reset it. So Start and Stop open and
close the `.raw` inside the running stream, exactly as Prophesee's own
recorder does, and the raw view only hides the live view. Two things
follow:

- a capture started **from the live view** can lack up to about 4 ms of
  events at the very start of its `.raw` (the SDK starts a file at its next
  time marker); a capture started straight after **Connect** lacks nothing;
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

### Exposure, gain and FPS

These three boxes are in the **Basic** tab under the image folder (see *The
settings tabs under the image folder* for the rest of the camera's
settings). Exposure and gain are applied when **Start capture** starts. **0 means "leave
it as the camera has it"** (a spin box always has a number in it):

- **Exposure µs** — up to 10 s. Basler only; an event camera has no exposure.
- **Gain dB** — up to 48. Basler only.

**FPS (0 = camera default)** is how many frames a second the camera records,
and it is applied **the moment you change it** — an arrow click, the mouse
wheel, or a typed number once you press Enter or leave the box — while
capturing too, not only at Start. (Start passes it again, so a rate chosen
before **Connect** is not lost; with no camera connected the status line just
says it will apply at Start.)

- On a **Basler** it caps the camera: 0 = as fast as it goes. If the camera
  cannot reach the rate at this exposure and area, the status line says what
  it will actually run at.
- On an **EVK4** it sets how long each picture collects events: 50 fps = 20 ms
  windows (the default), 200 fps = 5 ms, up to 1000 fps = 1 ms. The `.raw` has
  every event whatever this is, and the raw view uses the same window as the
  run's PNGs.

While the camera streams, the status line is rewritten thirty times a second,
so the new rate is shown *in* the live line for a few seconds, beside the
measured one — `13.2 fps (set to 25.0) · 28 grabbed · …` just after the
change — and the measured number reaches it within about a second (it is
averaged over the last second).

**While the camera is restarting for a change** (an area, a pixel format, a
preset — the status line says `Changing the camera's area…`), the camera
belongs to that change. A box changed meanwhile **waits** rather than writing
to a camera mid-restart: `FPS 25 is set once the camera has finished
changing the camera's area`, and it is set the moment that is done (the
newest value of each box; nothing waits past Disconnect). Not an error — an
arrow click during a restart is not something to fix.

Measured mid-capture through the function the box is wired to
(`frame_camera.apply_frame_rate`), on a 320 x 240 area:

| Camera | Box 10 → 25 → 10 → 0 | Saved |
|---|---|---|
| Simulated | 10.0 → 25.0 → 10.0 → 30.0 fps delivered | 188 of 188 |
| pylon emulator (`PYLON_CAMEMU=1`, `BaslerDevice`) | 9.9 → 25.0 → 10.0 → 62.5 fps delivered (0 = free-running) | 268 of 268 |

On the emulator `AcquisitionFrameRate` stays **writable while grabbing**
(`TLParamsLocked` = 1), and each change took under 0.4 ms. A real boost
camera's rate is also limited by exposure and the CoaXPress link — the status
line reports what the camera says it will reach.

### Settings → Python Scripts

**Settings ▾** (top right) drops a menu; **Python Scripts** opens a window
listing every Python file the app uses — for debugging, or for a bug report:

- the **Python** running the app (version and path) and the app's folder, at
  the top;
- **this app's own files**: `main.py` (starts the app), `app.py` (the window —
  written once, never regenerated), `handlers.py` (what each button does —
  yours to edit), `ui/*.py` (the generated layout — rewritten by every
  Generate, don't edit);
- the **Council modules it links to** (`frame_camera`, `gui_settings`,
  `frame_roi`, …) and **the ones those use** (`council_core.cameras`,
  `council_core.capture`, …), each with what it does and whether it has been
  loaded yet.

Type in the filter box to narrow it; **Copy all** puts the whole list,
interpreter first, on the clipboard; Ctrl+C copies selected rows; **Refresh**
looks again. The window is not modal — it stays open while the app captures.
The list is read from the files (nothing is imported to describe it) and,
measured in a running Typhon, takes about 0.25 s the first time and about
30 ms after that (the window opens in about 50 ms); the menu warms it up
while it is open. A file that does not parse says so in its row, with the
line — the likeliest thing you opened the window to find.

Any Designer project can have the same menu: put a button anywhere and, in
the **Wiring** panel, link it to `gui_settings` → `settings_menu`.

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
| `<run>_camera.json` | **The run's camera record**: which camera, where on its sensor the pictures are, and how it was set at Start (below). |

The `.raw` is the event camera's real data. A PNG is a 20 ms *picture* of it,
and PNGs can be skipped when storage falls behind; the `.raw` loses nothing.
It grows with how much is changing in the scene, so check free space before a
long run. A second run never overwrites the first — two runs started in the
same second get `_2`, `_3` on the name.

**The camera record.** A bird-bath run's PNGs are 160 x 120 — but which 160 x
120 of the sensor? `<run>_camera.json` says, written once at **Start** (after
the exposure, gain and FPS boxes are applied, before the run's first frame):

| Key | What it says |
|---|---|
| `camera` | backend, model, serial, vendor, kind, label — the camera as it identified itself when opened (an EVK4's model is the sensor it reports, e.g. `IMX636`) |
| `sensor`, `area`, `full_sensor` | the sensor's size, and the camera's own area (ROI) at Start in **sensor pixels** — where on the sensor every picture of the run is (a run keeps one area: see *One set-up per run*) |
| `settings`, `units`, `read_only` | every setting the camera described at Start (as the settings window keys them), the units, and which are readings (a temperature) |
| `preset` | the preset applied (or saved) last — **only while the camera is still as it left it**, checked at Start against every setting and the area; otherwise `""` and `preset_changed` says which preset and what no longer matches (an exposure typed after it, say) |
| `app` | the app that ran it: window title, Designer project, the example it was built from, when it was last generated |
| `software` | the Council's version, the source commit it runs from (read from its git folder, `""` in a build without one), the record writer's version, Python |
| `host`, `written`, `at` | which PC, and when |

It is plain JSON (`"format": "typhon-camera-record"`), written whole to a
temporary name and renamed into place, and **never written over**: a record
already there is left as it is. A record that cannot be written (a full disk)
is said in the status line, and the capture goes ahead — the frames come
first. Runs from before records simply have none.

### The slider

- **Connected, not capturing**, the slider has one position more than there
  are saved frames: its **right-hand end is live** (`Live · not saving ·
  drag back for 300 saved`). Drag back to look at a saved frame — `PNG 12 /
  300 · live at the end` — and the camera keeps streaming; Play runs to the
  end of the saved frames and on into live.
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
- **The raw view shows the same picture as the PNGs.** A `.raw` always holds
  the whole sensor's geometry (1280 x 720) with events in sensor
  coordinates, while a run made with the camera's own area has PNGs of that
  area alone. The raw view reads the area from the run's camera record and
  draws each window exactly as the live view drew it — the bird bath, not a
  1280 x 720 yard with only the bath active. A run from before camera records
  is shown whole, as before.

**Mark this frame** and **Predict this frame** use the PNG on screen; while
live or in the raw view there is no file on screen for them to use.

### The classifier: saved models

The right-hand column trains a small classifier on frames you mark ("good",
"bad timing", …) and applies it to a whole folder. Its models are **saved and
kept**, shared by every app on this PC, each saying where it came from.

- **Model** is a **dropdown of the saved models**, empty until you pick one
  (no model is chosen for you: in a shared store a model called `frames` may
  be another app's). Pick one to open it (its classes fill the list below),
  or type a new name and press Return — it is made when its first class is
  added. What you type is what Return opens: the box never finishes a name
  for you, so `bird` does not become `birds`, and ` birds ` or `BIRDS` opens
  `birds`. The open list shows each model's whole row: current version,
  classes, frames marked, **where it came from** (`from Typhon (birdlab)`,
  `from Barbie Capture v5 — live (barbie_lab)`), its tags, when it last
  changed. It is read again before it opens, before the arrow keys or the
  mouse wheel move through it, and after every classifier button — so a
  model another app saved a minute ago is there, and one renamed or deleted
  is gone. Only the files that changed are read again (about 0.3 ms a saved
  model, measured).
- **Show** narrows the list: *All classifiers*, *This app*, *App: …*,
  *Project: …*, *Tag: …*, *Origin unknown* (models made before origins were
  recorded) — or type part of a name.
- **New name** + **Save as** copies the open model (classes, marks, every
  version, its origin; not its run record) and opens the copy. **Rename**
  renames it (its versions, origin and run record go with it). **Delete**
  asks first — the button reads *Sure?* and the status line says what it does
  — and a second press within 4 seconds *moves* it to
  `<store>/.deleted/<name>_<time>` and says so; move it back to restore it.
  None of them ever overwrites a model: a taken name is asked about, never
  replaced. *New name* is emptied once a name is used, and kept when the press
  asks for another.
- **A slip never empties the window.** A name that cannot be one (`my birds`,
  `a/b`), a taken name, a file to import that is not there or not an export,
  or another app's model refusing a change: each is answered in the status
  line, with the open model, its classes and what you typed left as they
  were.
- **Export** writes the open model as **one file**,
  `<name>-v<N>.typhon-classifier.zip`, into the folder beside it (the box
  that says *Folder to export into*; a folder that is not there is refused,
  never taken for a file name) — the model, its marks, its origin, tags and
  its record of what it classified; everything another PC needs, without the
  frames. **Import** reads such a file back — the one chosen in the box that
  says *Classifier .zip to import* — under its own name, or the name typed
  in *New name* (the status line then says what the file calls it), checking
  all of it first; it never overwrites. **Export this app's classifiers**
  writes everything that **belongs to** this app into one bundle,
  `<project>-<time>.typhon-classifiers.zip`, which **Import** also reads.
- **Tag** + **Add tag** / **Remove tag**: words of your own (`rig A`,
  `night shift`) that *Show* can filter by; they never change the origin.
- **Train** makes a new **version** (`birds v3 (1a2b3c4d)` — number and the
  first 8 characters of its checksum) only when the marks changed; every
  version is kept.
- **"Classified with"** — the note under the library — says which model
  version last classified the frames in the **capture folder**, and when:
  `Classified with birds v1 (98f54722) on 2026-10-05 15:54 — good 5, bad
  timing 3`. **Classify all frames** records each run it classifies (in the
  store — the capture folder is only ever read). The note is per **run**: a
  run captured into the folder since then is added — `· not classified yet:
  run 20261005_130000 (8 frames)`. A renamed model is named as it is called
  now (`hawks v1 (98f54722; then called birds)`), a deleted one still answers
  (`since deleted`), and another PC's records never answer for a folder here.
  The note is read again when the folder box changes, when a capture's last
  frame is on disk, and after every classifier button (a failed one puts it
  back rather than leaving it blank).

**Whose it is.** Every shipped app calls its first model `frames`, so in a
shared store one app's could be another's by accident. Only the app a model
belongs to — the one that made it, or made its copy or import — may **Add
class**, **Remove class**, **Mark** or **Train** it; any app may open it,
predict and classify with it, tag it, copy it and export it. Another app's
model says whose it is and offers the two ways on: **Save as** (a copy of
your own, keeping where it came from) or the tag `shared` (every app may
change it). The same goes for **Rename** and **Delete**: Barbie would find
its model gone. "This app" and **Export this app's classifiers** go by whose
a model is, not where it came from: Typhon's own copy of Barbie's model is
Typhon's (and Barbie's "This app" no longer lists it) — so a Typhon going its
own way takes every model it can change.

**Where they are kept.** In **one store**, `<vault>/classifiers` (the
Council's vault, `~/.council/vault` unless `COUNCIL_VAULT_ROOT` says
otherwise) — never beside the frames. An app that leaves the Council
(Typhon spun off on its own, or a GUI made later) keeps classifiers of its
own: set `FRAME_CLASSES_STORE`, or put `classifier_store.json`
(`{"store": "classifiers"}`, relative to that file) beside the app; then
**Import** the bundle **Export this app's classifiers** wrote from the shared
store. Each import adds itself to the model's history; the origin stays where
it was made.

### Two boxes: the crop box and the camera's ROI

They are different things, in different pixels, and Typhon keeps them apart:

| | The **crop box** (the *Crop box* entry) | The **camera's ROI** (its own area) |
|---|---|---|
| Pixels | of the **picture** it was drawn on | of the **sensor** |
| Shown in | the *Crop box (x, y, w, h)* entry | the line under *Apply area to camera* (`Camera's area: 320, 200, 160, 120 of 640x480, sensor px`) and the settings window |
| Used by | **Save cropped frames** — cuts saved PNGs | the camera itself: an EVK4 emits events **only inside it**, a Basler **reads out only it** |
| Changes the recording? | no — the originals are untouched | yes — frames (and the `.raw`) hold only that area |
| Kept in a preset? | no | yes |

**How the coordinates work.** A box is drawn on the picture, so it is in
*picture* pixels. The live picture of a camera with an ROI set **is** that
ROI, so **Apply area to camera** adds the ROI's own origin (the `x, y` of the
area the frame on screen was taken with) to turn the box into *sensor*
pixels: on a picture of the area `100, 60, 320, 200`, a box drawn at
`8, 10, 64, 32` becomes the sensor area `108, 70, 64, 32`. The crop box is
never converted — **Save cropped frames** cuts the saved PNGs in their own
pixels. The settings window's area box and a preset's area are always
sensor pixels; a Basler's are in its binned pixels when binning is on.

### Cropping to a region and saving it

Draw a box on the picture with **Draw ROI** (or type `x, y, w, h` into the
**Crop box**), choose **Save cropped frames to**, and press **Save cropped frames**:
every PNG in the capture folder is cropped to that box and saved there, with
the originals untouched. It works on the PNGs — during a capture, after it, on
an EVK4 run as well as a Basler one. It does not crop the `.raw`.

### The camera's area (both cameras)

Draw a box on the **live** picture (or type it, in the picture's pixels),
then **Apply area to camera**. This sets the camera's **own** ROI — an EVK4
then emits events only there (the bird bath, not the whole back yard), a
Basler reads out only that window — so frames arrive at that size and are
saved at that size. It is snapped to what the sensor accepts, and the status
line says so when it had to move. **Full sensor** puts it back.

- **A box drawn on an area is inside that area.** The live picture of a
  camera with an area set *is* that area, so Typhon adds the area's own
  origin before telling the camera: draw a smaller box inside the new
  picture and it lands where you drew it. (Before, it was sent as it was and
  landed somewhere else on the sensor.)
- **Draw it on the live picture.** A box drawn on a saved frame is refused —
  that frame may have been taken with another area. Drag the slider to its
  end first. A box that runs past the live picture is refused too: it was not
  drawn on it (usually the camera's own area, in sensor pixels, left in the
  crop box by an older Typhon's hand-edited Connect or Apply-area handler,
  which *Update from example* keeps — and warns about). Before, each press
  moved the area by its own origin again.
- **An area that is not on the sensor is refused, not moved.** A preset's
  area, or one typed in sensor pixels, that lies wholly outside this camera's
  sensor (a hand-edited file, a preset from a camera with a bigger sensor)
  leaves the camera's area as it is and says so; the preset's settings still
  apply. Before, it was pulled into the sensor's corner — another part of the
  scene — and called "snapped". An area that only runs past the edge is still
  fitted onto the sensor, as before.
- The crop box is **cleared** when the camera's area changes (the box was in
  the old picture's pixels), and Connect no longer fills it with the camera's
  area — that was the sensor's numbers in a box that means picture pixels.
- The line under the two buttons always says the camera's area **now**, in
  sensor pixels — whichever window changed it. That includes a Basler's
  **binning**: its area is in binned pixels, so changing the binning changes
  the area's numbers; the line, the settings window's area box and the
  status line say the new ones, and the crop box is cleared. (Checked on the
  simulated camera only: pylon's emulator ignores binning.)
- To type the area in sensor pixels instead, use the settings window's
  **Camera's area** box.
- **Not while capturing**, on either camera — see *One set-up per run*.

### The settings tabs under the image folder

Under the **Image folder** box is a row of tabs that holds the camera's
settings in the main window — no window to open for the everyday ones.

- **Basic** is always there: the **Exposure**, **Gain** and **FPS** boxes
  (they used to sit on their own under the folder; they work exactly as
  before — see *Exposure, gain and FPS*). Connected to an event camera,
  Exposure and Gain are **off and look it** — dark, dashed, and saying
  `n/a — event camera` (an event camera has neither) — and the note under
  them says so; FPS still sets its picture window.
- **The boxes follow the tabs.** When a tab, a pop-out, a preset or a reset
  changes the exposure, the gain or the frame rate (an event camera's
  picture window), the box beside Start shows the camera's new value — whole
  µs, dB or fps, and 0 ("keep") while an auto mode owns it. That is the
  camera's value, not a new word from you: Start leaves the camera as it is
  and says nothing about it. Type in the box afterwards and Start writes
  what you typed, as before.
- When a camera **connects**, a tab per category of **that camera's own
  settings** appears beside Basic, built from what the camera describes —
  so an EVK4, a Basler and the simulated cameras each get their own set, and
  a model with more features gets more rows, never fewer. **Disconnect**, or
  connecting another camera, rebuilds them; with no camera the tabs say
  *Connect a camera to see its settings* (the Camera tab is shown then, if a
  camera tab was). Connecting again brings back the camera tab you last
  chose — Biases stays Biases across a reconnect — or, on a camera without
  it, that camera's first tab; Basic and Presets stay where they are.
  - **EVK4**: **Biases** (`bias_diff_on`, `bias_diff_off`, `bias_fo`,
    `bias_hpf`, `bias_refr`, each with the sensor's own range, and a green
    bar under the slider marking the range the sensor **recommends**),
    **Filters** (the event rate controller, anti-flicker, the event trail /
    STC filter and the event rate activity filter, each a section of its
    own), **Display** (below), **Camera** (its area, then the readings —
    temperature, illumination, pixel dead time — and what it is: serial,
    sensor, event format).
  - **Basler**: **Exposure** (exposure and auto exposure, gain and auto gain,
    black level, the frame-rate limit and the rate the camera will reach,
    and the trigger: mode, source, edge, delay), **Image** (pixel format,
    mirror X/Y, gamma, digital shift, binning and its mode), **Camera** (its
    area, and its temperature where the model reports one) — whichever of
    these nodes the model has.
  - **Simulated cameras**: their small set, the same way.
- Last, **Presets** (below).

A tab is a **glance and a quick change**: each section shows its category's
most-used settings, compactly, and says how many more there are
(`+4 more in Pop out: Filter, Duty cycle, Start threshold, …`). Everything in a tab is
**live**, exactly as in the settings window: a slider writes the newest
value at most every 60 ms while it moves and always the last one, so the
live picture follows the drag; what the camera took is what is shown.

**The mouse wheel scrolls the tab** (and a pop-out), whatever is under the
pointer: a slider, box or list takes the wheel only **once you have clicked
it** (or reached it with Tab). Before, wheeling down the Filters tab moved
the trail filter's threshold on the camera instead of the page, and one
notch over the pixel format changed it. A setting that restarts the live
view (pixel format, mirror, binning) never changes from the wheel at all —
pick it from its list.

**Display — what an event picture looks like.** The live view used only the
defaults; these are now settings of the event camera (the simulated event
camera has them too):

- **Picture window** — how long each picture collects events (the same thing
  the FPS box sets: 50 fps = 20 ms);
- **Events shown** — `ON and OFF`, `ON only` or `OFF only`;
- **Colours** — `Grey` (mid-grey ground, white ON, black OFF: the picture
  every earlier run saved), `Dark` (the events on black — easier to read in a
  sparse scene) or `Colour` (light ON and blue OFF on dark blue; the PNGs are
  then saved in colour).

They change the **live view and the PNGs only — never the `.raw`**, which has
every event whatever they are (the raw view draws it in the standard grey).
They are saved in presets and in each run's `<run>_camera.json`, so a run
says how its PNGs were drawn.

### Pop out a category

**Pop out ⧉** beside a section opens **that category** — every one of its
settings — in a window of its own beside Typhon. Not a dialog: the live view
and any capture carry on, and **several can be open at once** (Biases on one
side, Anti-flicker on the other); pressing Pop out again brings the open one
forward. A pop-out opens **as wide as its rows** (the range and the ↺ beside
each never need a sideways scroll), and cannot be made narrower than that.

- Every setting of the category, with the **camera's own range and unit**
  beside it (`-85 … 140`, and under it `rec. -25 … 60` for a bias), as the
  camera reports them.
- **Live**: drag a bias and the live picture shows it as you drag, through
  the same 60 ms throttle — the camera is not sent a value per pixel of the
  drag.
- **Everything stays in step**: a change in a pop-out moves the same row in
  the tabs and in any other window showing it, and the FPS box (or Start
  writing the exposure and gain boxes) moves the rows it sets — the views
  all hear the one answer the camera gave.
- A setting **the stream is in the way of** (a Basler's pixel format, mirror,
  binning) says `Changing this restarts the live view for a moment`; its
  slider writes **once, when you let go**, not at every step of a drag (each
  write would stop and restart the stream). While **capturing** it is greyed
  with `Stop the capture to change this — one run keeps one set-up`, and
  frame_camera refuses it with the capture's message anyway.
- A setting **another one owns** is greyed and says why
  (`Greyed out while "Auto exposure" is Continuous — change that
  first`, by the label of the row that owns it); a
  **read-only** one says `Read only — …`; a category of readings (Status)
  says so once at the top.
- **Readings stay current**: the temperature, illumination and pixel dead
  time (a Basler's temperature) are read again **every 2 s while they are on
  screen** — the Status pop-out open, the Camera tab showing, the settings
  window open — and only they are read (three facility calls on an EVK4).
  Out of sight they are not read at all.
- **Reset <category> (as connected)** puts only this category back as the
  camera had it when it was connected — the rest are left alone. While
  capturing it works when everything in the category changes live (it is
  the same as dragging each back by hand); in a category with a setting the
  stream is in the way of that is not as connected, it is **greyed until
  Stop**, its tooltip saying why.
- A **preset bar** at the bottom: pick a preset and **Apply**, or type a name
  and **Save** — the **whole camera** (every setting and its area), not only
  this category; a pop-out is where a set-up gets tuned, so it is where it
  can be saved.

### Saving a configuration — presets, export and import

A **preset** is the camera's whole set-up: every setting worth saving
(including an event camera's Display settings) **and the camera's own area**,
kept per camera **in the app's project folder** (`camera_presets.json`), as
described under *Camera settings and presets*. Three places save and apply
them, and they always list the same presets:

- the **Presets** tab: the list; **Apply** (or double-click), **Rename**,
  **Delete** (click twice); a name + **Save as** (tick *with the camera's
  area* to include it); **Export…** and **Import…**;
- each pop-out's preset bar (above);
- the preset box and **Save preset** under the status line.

**Export…** writes the preset chosen in the list to a file of its own,
`<name>.camera-preset.json`, in a folder you pick — to take to **another
project or another PC**. It **never writes over a file**: a name already
there becomes `<name>_2.camera-preset.json` (the file is created
exclusively, so not even a file that appears at the same moment is
replaced).

**Import…** adds the preset in such a file to this camera's presets **in
this project**. It is checked as strictly as the presets file itself — not
JSON, not an exported preset (a project's whole `camera_presets.json` is
refused with a word on what to export instead), bigger than 1 MB, saved by a
newer version of the app, or a value of the wrong kind: each refused with
the reason, and nothing is written. It **never replaces a preset**: a name
already used here gets ` (2)` and the status line says so. A preset saved on
the **other kind** of camera (an event camera's biases for a Basler) is
refused — none of it could apply; one from **another model of the same
kind** is imported, and the status line names the settings this camera does
not have (they are reported, not applied, when you apply it). Importing does
not apply it: it is chosen in the list, with its name (`Bright (2)`) in the
name box — apply it from there.

**Applying a preset says what it took.** It is applied **live** where it can
be — the status line ends `Applied live.` — and where something in it needs
the stream stopped, the live view restarts once for all of it and the line
names what needed that: `The live view restarted for Pixel format (the
stream must stop to change it).` Applying a preset, like changing the area,
waits for **Stop** while capturing (one run, one set-up); **saving** one
works while capturing.

**What the tabs and pop-outs cost the window** — offscreen on this PC, the
window not on any screen but painted into its offscreen buffer, four runs
on a simulated frame camera at **5328 x 3040** (the boA5320's size, Mono8,
30 fps), dragging Gain back and forth for 4 s at 60 positions a second:

| UI thread | Tabs only | Dragging in the tab | Dragging in a pop-out (the tab open too) | 3 pop-outs open, dragging |
|---|---|---|---|---|
| live view, per tick (median / worst) | 1.8–2.0 / 2.4–3.1 ms | 1.8–2.0 / 2.8–4.7 ms | 1.8–2.0 / 2.8–3.1 ms | 1.9–2.0 / 2.9–3.3 ms |
| one setting written, the whole cycle (median / worst) | — | 0.34–0.36 / 0.6–2.1 ms | 0.51–0.53 / 1.0–2.2 ms | 0.53–0.55 / 1.0–1.7 ms |
| pictures drawn a second | 27.0–28.3 | 28.2 | 26.7–28.2 | 26.9–29.1 |
| longest the window was busy | 7–15 ms | 9–11 ms | 7–10 ms | 8–10 ms |

About 60 writes reach the camera in the 4 s of dragging (the throttle), and
each view reads its settings again once, when the drag stops (0.1–0.2 ms
here). The "longest busy" column is the largest gap seen by a 2 ms timer,
and the longest one (15 ms) came with nothing being dragged — the live
view's own 16 MB picture, not the settings. On **pylon's emulator** (a real
`BaslerDevice`, its largest frame 4096 x 3040): 30.3–30.4 pictures a second
and 0.26–0.29 ms a tick in every case, 0.6–0.97 ms per written gain; a fake
EVK4 at 1280 x 720: 0.25–0.27 ms a tick, 0.34–0.59 ms per written bias.

**What is read from the camera, and when.** On an EVK4 every facility read
is a USB round trip, so each view reads only what a change can have moved:
after a drag stops, a tab reads **its own tab's** categories and a pop-out
**its own category**; the FPS box (and Start writing the boxes) makes the
views read only the rows of what it wrote — an EVK4's picture window, which
is the app's own setting, costs the camera nothing. Connect describes the
camera **once**, for its as-connected values and for every view. Counted on
the fake EVK4 (every facility call): Connect **46** calls (it was 132 with
the tabs before this, 46 before the tabs); one FPS arrow click **0** (it was
43); a bias written in its tab reads the Biases facility only. A **closed**
pop-out (or settings window) reads nothing at all until it is opened again,
when it reads its category afresh. **Not measured: a real EVK4.**

**Smallest window.** At Typhon's smallest size (1400 x 820) the column is
431 px wide and a tab page 190 px high: every tab title fits (an EVK4's six
need 419 px), nothing on a page is wider than the page, and a page scrolls
down to what does not fit (checked with Arial, offscreen, for each camera).

### Camera settings and presets

**Camera settings…** (under the status line) opens a window beside Typhon —
not a dialog: the live view and any capture carry on underneath. It lists
every setting the connected camera has; the camera describes them itself,
with its own ranges, increments and units, so nothing is guessed:

- **EVK4** (through the Metavision HAL, as Metavision Studio shows them): every
  bias (`bias_diff_on`, `bias_diff_off`, `bias_fo`, `bias_hpf`, `bias_refr`, …,
  with the allowed and the recommended range the sensor reports), the **event
  rate controller** (on/off, rate limit), **anti-flicker** (on/off, band pass /
  band stop, the frequency band, duty cycle, thresholds), the **event trail
  filter** (on/off, TRAIL / STC_CUT_TRAIL / STC_KEEP_TRAIL, threshold), the
  **event rate activity filter** where the sensor has one, the **display**
  (the picture window in ms per picture, which events are shown, the
  colours — see *The settings tabs under the image folder*), and read-only
  status: temperature, pixel dead time,
  serial, sensor, event format. A `.raw` opened as a device has none of the
  facilities and lists only what it has.
- **Basler** (from the node map): exposure (and auto), gain (and auto), black
  level, gamma, digital shift, pixel format, mirror X/Y, binning and its mode,
  decimation, the frame-rate limit, sensor readout mode, and read-only the
  frame rate the camera will reach and its temperature — whichever the model
  has, with the ranges its nodes report.

**The window.** Settings are grouped as the camera groups them (Biases,
Event rate controller, … / Exposure, Gain, Image, Binning, Frame rate), one
row each: a slider and a box for a number (the slider is logarithmic for a
long range such as an exposure of 20 µs … 10 s), a tick box for on/off, a
list for a choice, plain text for a reading. On the right: the **camera's
area** in sensor pixels, the **presets**, and **What the last change did**.

- **Changes apply as you make them.** Dragging a slider writes the newest
  value at most every 60 ms, and always the last one, so the live picture
  follows the drag; a typed number is written when you press Return or
  leave the box, never per keystroke.
- **What the camera took is what is shown.** A value outside the range is
  clamped before it is sent (pylon refuses rather than clamps), the camera
  may snap it (an exposure of 5003.7 µs runs as 5004), an EVK4 may refuse a
  bias — the control shows the camera's value and the row says
  `The camera made it 5004 (asked 5003.7)` or `NOT changed — …`.
- A bias outside its **recommended** range says so under its row.
- A setting another one owns is **greyed out and says why** (`Greyed out
  while "Auto exposure" is Continuous — change that first`). One the stream is
  in the way of (a Basler's pixel format, mirror, binning) says `Changing
  this restarts the live view for a moment`, and does exactly that.
- **↺** on a row puts that setting back **as the camera had it when it was
  connected**; **Reset all (as connected)** puts them all back (not the
  area — **Full sensor** is for that). An EVK4 is opened with its sensor's
  default biases, so for an EVK4 this *is* the camera's default.
- **Camera defaults** (a Basler, when its node map has `UserSetSelector`
  "Default" and `UserSetLoad`) loads the camera's **own factory set** —
  every setting and the area — with the live view stopped for it. The
  emulator has the nodes but ignores the load, so this button is checked
  against the node names only; an EVK4 has no such set and shows no button.

**Presets.** A preset is one camera set-up: every setting worth saving **and
the camera's ROI** (sensor pixels), or — untick *with the camera's area* —
the settings alone. In the window: type a name and **Save as** (a preset of
the same name is replaced; it works while capturing, as it only reads the
camera), pick one and **Apply** (or double-click it), **Rename to the name
below**, **Delete** (click twice — it asks with the button, not a dialog).
In Typhon's own window, under the status line:

- the **preset box** lists this camera's presets — **picking one applies
  it**;
- type a new name into the same box and press **Save preset** to save the
  camera as it is now (its settings and its area) under that name. (Return
  on a name that is not a preset yet only says so — it is not an error.)

Both windows always list the same presets, whichever saved one.

Presets are kept **in the app's project folder**, in `camera_presets.json`
beside `camera_setup.json` — copy the project and they go with it — per
camera (model and serial): a camera of the same model that has no preset
of that name is offered another unit's, marked as such (it can be applied
here, not renamed or deleted from here). **An event camera's model is the
sensor it reports when it is opened** (`IMX636`, `Gen4.1`, `GenX320` — read
from the camera, never assumed), so biases tuned for one Prophesee sensor
are never offered to another as "the same model". The scan list shows
`Prophesee (<serial>)` until **Connect** has asked the camera; **Connected
to Prophesee IMX636 (<serial>)** after. Presets an older Typhon saved for an
EVK4 (it called every Metavision camera "EVK4") are still that camera's own
by its serial, and are moved under its sensor's name the next time one of
them changes. The file is plain JSON, written
safely (whole, then renamed into place); a damaged one is left exactly as it
is and named — saving a preset then keeps it as
`camera_presets.damaged-<time>.json` and starts a new one. A project folder
nested so deep that the file's path passes Windows' 260 characters says so,
rather than "No such file or directory".

Applying a preset writes its settings in an order that works (auto modes off
before the values they own, pixel format and binning before the area, the
area before the frame-rate limit, a filter's parameters before it is
switched on), then the area. Settings that cannot change while a Basler
streams (pixel format, mirror, binning, the area) stop the live view for a
moment and start it again. A very slow camera (a frame every half second)
does not freeze the window: after 0.15 s it says `Applying…`, greys itself,
and the answer arrives in the status line and the window when the change is
done. An EVK4 sets everything live.

**What it costs the window** (offscreen, this PC; ranges over five runs —
the PC was busier in some, which moved every number together):

| | UI thread |
|---|---|
| picking a preset in Typhon (pixel format + area: the live view restarts) — simulated camera / pylon emulator | 16–34 ms / 16 ms, once |
| applying a preset that changes the area of a **5328 x 3040** live view | 7–47 ms, once (the stream stops and restarts) |
| one setting written from a dragged slider (the whole cycle: the camera call and the window's handling) | 0.35–0.6 ms median, 1.4 ms worst; 64 writes in 4 s of dragging |
| the live view at **5328 x 3040** (Mono8), per tick | 1.9–3.3 ms median, 2.5–4.8 ms p99 — the same with the settings window open and a slider being dragged |
| pictures drawn a second at 5328 x 3040, window open or not | 26–28.5 |

Before the window stopped re-styling every row on every write, the longest
stall during a drag was 39 ms; it is now 5.7–9 ms. Single slow ticks of
26–87 ms still turned up in two of the five runs, with **and** without the
window open — the live view's own (a 16 MB picture), not the window's.

**The first live picture after Connect no longer freezes the window.** The
picture libraries (Pillow's ~70 image plugins) must be loaded before a
camera starts streaming; that used to happen on the window's thread at the
first live tick — measured in a built Typhon, the first tick after Connect
took 110–307 ms (simulated frame camera) and 113–483 ms (simulated event
camera), each time the longest stall after Connect, and longer on a cold
start. They are now loaded on a thread of their own as soon as the window
opens, and the live view waits a tick for them if Connect comes first: the
first tick after Connect is 6–8 ms, and it is still the longest of the next
three seconds (five fresh starts per camera, offscreen).

### The bird bath

The user's case: an EVK4 looking at a back yard, the camera never moved, and
only the bird bath matters.

1. **Connect**. The picture is live; nothing is saved.
2. Draw a box around the bird bath on the live picture (**Draw ROI**), then
   **Apply area to camera**. The camera now emits events **only there**: the
   picture becomes that part of the yard, the `.raw` and the PNGs will hold
   only it, and the line under the buttons says
   `Camera's area: 512, 300, 160, 120 of 1280x720, sensor px`. Drawn too
   big? Draw a smaller box on the new picture and apply again — it lands
   where you drew it.
3. The **Biases** and **Filters** tabs under the image folder (or **Pop
   out** a category, or **Camera settings…**) — set the biases
   (`bias_diff_on` / `bias_diff_off` for how strong a change must be), the
   trail filter, anti-flicker for a pump or a lamp; **Display** if the
   events read better on black. The picture shows each change as you make
   it.
4. Type `Bird bath` into the preset box and press **Save preset**.
5. Another day — another run of Typhon, the same project: **Connect**, pick
   **Bird bath** in the preset box. The biases, filters and the camera's ROI
   are back; press **Start capture**.
6. The run's `<run>_camera.json` says `"preset": "Bird bath"` and the area
   `512, 300, 160, 120` of the 1280 x 720 sensor — where its 160 x 120
   pictures came from — and **PNG / Raw** shows the `.raw` as that same
   area.

### One set-up per run

While **capturing**, the camera's area, a preset, and any setting the
stream is in the way of (a Basler's pixel format, mirror, binning) are
**refused** with `stop the capture before …` — on both cameras. Before, a
Basler's area change restarted the stream *into the same run*, leaving one
run with frames of two sizes; an EVK4's `.raw` would change meaning
halfway. Settings that change live — exposure, gain, an EVK4 bias, the FPS
box — still apply mid-capture. Saving a preset only reads the camera, so it
works while capturing too.

### Reading the status line

`60.0 fps · 242 grabbed · 157 not drawn (screen only) · 242 saved`

- **lost by the camera/driver** (Basler) counts frames pylon itself threw away
  before the app saw them — invisible before this. While **recording**, pylon
  queues frames (about 1 GB of them) instead of keeping only the newest, so
  this should stay at zero; it can appear in the live view, which only ever
  wants the newest frame. If it grows while recording, the link or the PC
  cannot keep up — the Basler scan's link checks say which.
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

**When the camera sends nothing**, the line says so instead of a rate:
`Live view — not saving · NO PICTURE for 6 s — the camera waits for a
trigger: its FrameStart trigger is On (source Software) — nothing here sends
a software trigger: set Trigger mode Off (Exposure tab, Trigger)`. (The rate
is measured from frames that arrive, so with none arriving it used to keep
showing the last one — `62.5 fps` for a camera that had stopped.) A camera
running slowly is not called silent until several of its own frame intervals
have passed with nothing.

**A Basler's trigger.** With **Trigger mode On** the camera waits for a
trigger before each frame, and the picture stops until one arrives — one
click in the Exposure tab does it (measured on pylon's emulator, whose
source is `Software`). So:

- the **Trigger mode** row says it in orange under itself — `No picture: it
  waits for a software trigger, and nothing here sends one — set it Off`, or
  `No picture until a trigger arrives on Line1` — and the status line says
  it with the change;
- the live line says how long there has been no picture and which trigger
  it waits for (above);
- **Start capture is refused** while a trigger waits for **Software** —
  nothing in Typhon sends one, so the run would save nothing; with a
  trigger on an **input line** the run starts, says which trigger it waits
  for, and after Stop says `the camera waited for a trigger the whole run`
  if none came;
- a Basler has **one trigger per TriggerSelector entry** (nine on the
  emulator: FrameStart, FrameBurstStart, …), and the rows show the
  **selected** one only. Every trigger counts above — a FrameBurstStart
  left On while the selector shows FrameStart is named — and **Reset Trigger
  (as connected)** / **Reset all** put **every** trigger's mode back as
  connected, saying which (`the FrameBurstStart trigger Off again`). A preset
  keeps the selected trigger only.

Each run writes under its own timestamped stem, so a second run into the same
folder can never overwrite the first. Unplugging the camera mid-capture ends
the capture with one clear message rather than an endless stream of errors.

## Pixel depth

A Mono12 frame arrives as `uint16` holding 0–4095 and is saved as a 16-bit PNG
with those values **unaltered** (verified lossless).

| Basler pixel format | Saved as |
|---|---|
| Mono8, Mono10/12/16 (and their packed forms) | 8- or 16-bit grey PNG, lossless |
| RGB8, BGR8 | colour PNG, lossless — BGR8 is put red-first as it is saved |
| Bayer (colour cameras' raw) | the raw mosaic as a grey PNG — lossless, not in colour |
| BGRA8, RGB16, YUV, 10/12-bit colour | converted to 8-bit RGB as each frame is saved (extra CPU; 16-bit colour loses its low bits) |
| anything pylon cannot convert | the capture stops with a message naming the format |

On screen, a 16-bit frame is shifted down by the bits it actually uses, and
that shift only grows (so a dark frame never flashes brighter than its
neighbours) — **within one pixel format**. Each frame says the format it was
taken in, so changing Mono16 → Mono12 → Mono10 in the settings window or with
a preset starts the shift again; before, the live view kept Mono16's shift and
showed Mono12 at 1/16 of its brightness (measured on the emulator). Saved
frames are shifted per run, so a Mono16 run and a Mono10 run in one folder
each fill the display when reviewed.

The Basler scan shows this for every format the connected camera offers. One known consequence:
`frame_classes.thumbnail` scales 16-bit input as if it filled the full range,
so a 12-bit frame reads dark *to the classifier*.

## If something goes wrong

| What you see | What it means |
|---|---|
| Basler: empty camera list | Rerun **Camera setup…** and read the **Basler scan** at the top: missing CoaXPress support, a driver problem, a card in the wrong slot or without power, a GigE camera on another subnet. |
| Basler: `lost by the camera/driver` in the status line | pylon dropped frames before the app got them. Zero while recording is normal; if not, run the Basler scan and read the link checks (CXP cables and applet, USB port, GigE packet size and driver). |
| "could not open … something else is holding the frame grabber" | The pylon Viewer, or a previous run, still owns the card. Close it. |
| EVK4: empty list with the camera plugged in | The USB driver, almost always (Prophesee step 3). The SDK cannot tell "no driver" from "no camera" — both are an empty list. Other causes that look identical: Metavision Studio or another program has the camera open (close it); on an OpenEB build, `MV_HAL_PLUGIN_PATH` not set or `libusb-1.0.dll` not on `PATH` (run the build's `utils\scripts\setup_env.bat` first); camera firmware older than 3.8. |
| EVK4: finding out *why* it was skipped | Start the app with `MV_LOG_LEVEL=TRACE` set and read the console — the SDK only explains a skipped camera there. |
| EVK4: connected, but hardly any events | Expected with a still camera on a still scene: an event camera only reports *change*. Wave a hand in front of it. |
| EVK4: "Metavision SDK installed" fails | Not installed, or installed for a different Python than the app runs under (3.10–3.12 only). |
| Camera connects, live view stays blank | Check the AOI — **Full sensor** resets it. |
| The box under Gain says **Frame count**, or there is no **Settings** button | Your Typhon was built from an older example. **GUI Designer → Open → Update from example…** (see *Already have a Typhon?*). |
| No preset box, **Save preset** or **Camera settings…** under the status line | The same: **Update from example…** brings them; your own handlers are kept. |
| The classifier name is a plain box, not a dropdown of saved models; no **Save as / Export / Import** | The same: **Update from example…** brings the library. The old name box becomes the dropdown — named afresh as a fresh build names it (`cmb_model`; the update's log says so) — and starts empty: pick your model in it. |
| "'frames' belongs to Barbie … not to this app" on **Add class**, **Remove class**, **Mark**, **Train**, **Rename** or **Delete** | The model was made by another app sharing the store. **Save as** a copy of your own, or tag it `shared` (Tag + **Add tag**) to let every app change it. |
| **Delete** did nothing but say "Press Delete again" | It asks first: press it again within 4 seconds, on the same model. |
| The model dropdown is empty and its tooltip says the saved models cannot be read | A `classifier_store.json` beside the app names no folder, or cannot be read; fix it or move it away (nothing is looked for elsewhere on purpose). |
| **Import** says a name is taken | Nothing was overwritten. Type another name in **New name** (for a bundle, a prefix such as `lab2-`) and press **Import** again. |
| "classified with" says `Not classified yet` after classifying on another PC | Run records are per PC: a folder is a path on one computer. **Classify all frames** here records it here. |
| The run's `_camera.json` is missing | A run from before camera records, or the status line at **Start** said the record was NOT written (a full disk). The raw view then shows the whole sensor. |
| A box beside Start says `… is set once the camera has finished …` | A change was restarting the camera's stream; the value is written when it is done. |
| **Camera settings…** says `Connect a camera first` | The window lists what the camera describes, so it needs a connected camera. It stays open across Disconnect / Connect and fills in again. |
| Saving a preset says the path is past 260 characters | The project folder is nested too deep for Windows. Move the project somewhere shorter (or enable long paths in Windows). |
| Which file does what, or which Python is this? | **Settings → Python Scripts**. |
| `NOT saved (storage too slow)` in the status line | The folder is on a disk (usually a network share) that cannot keep up. Capture to a local folder and copy the run afterwards. |
| **PNG / Raw** says `Raw opens after Stop` | The `.raw` is still being recorded. Stop the capture first. |
| **PNG / Raw** says `No raw file for this run` | A Basler run (only event cameras record a `.raw`), or the `.raw` was not copied along with the PNGs. |
| **PNG / Raw** says the raw view needs the Metavision SDK | Viewing a `.raw` uses Prophesee's SDK; install it on this computer (Camera setup…, EVK4). |
| "stop the capture before changing the camera's area" (or "… applying a preset") | One run keeps one camera set-up — see *One set-up per run*. Stop, change it, Start again. |
| "that box is on a saved frame" | **Apply area to camera** uses a box drawn on the live picture. Drag the slider to its end and draw it there. |
| "the box … runs past the live picture" | The crop box holds something not drawn on this picture — often the camera's own area left there by an older, hand-edited handler (see the Update log's WARNING lines). Draw the box again on the live picture. |
| "the area … lies outside this camera's … sensor" | A preset's (or a typed) area is not on this sensor at all. The settings applied; the area was left as it is. Draw the area again and save the preset over the old one. |
| "… — wait for it to finish" | A slow camera is still restarting after a change; the status line says when it is done. |

For an EVK4 the **official installer is the lower-risk route**: it installs the
USB driver and registers where its plugins live, which removes three of the
silent causes above. OpenEB works, but those three become your job.
