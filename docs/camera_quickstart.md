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
**Save preset** or **Camera settings…** button. To bring it up to date
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
3. Optionally set the camera up: **Camera settings…** for every setting it
   has, a box drawn on the picture + **Apply area to camera** for its own
   ROI — or simply **pick a preset** in the box under the status line. See
   *Camera settings and presets* and *The bird bath* below.
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

Exposure and gain are applied when **Start capture** starts. **0 means "leave
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

The `.raw` is the event camera's real data. A PNG is a 20 ms *picture* of it,
and PNGs can be skipped when storage falls behind; the `.raw` loses nothing.
It grows with how much is changing in the scene, so check free space before a
long run. A second run never overwrites the first — two runs started in the
same second get `_2`, `_3` on the name.

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

**Mark this frame** and **Predict this frame** use the PNG on screen; while
live or in the raw view there is no file on screen for them to use.

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
  **event rate activity filter** where the sensor has one, the picture window
  (ms per picture), and read-only status: temperature, pixel dead time,
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
  while ExposureAuto is Continuous — change that first`). One the stream is
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
here, not renamed or deleted from here). The file is plain JSON, written
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

The first live picture after **Connect** costs about 0.6 s once: the camera
libraries are imported on the window's thread before the grab thread starts
(capture.warm_imports), so the two never race for Python's import lock.

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
3. **Camera settings…** — set the biases (`bias_diff_on` / `bias_diff_off`
   for how strong a change must be), the trail filter, anti-flicker for a
   pump or a lamp. The picture shows each change as you make it.
4. Type `Bird bath` into the preset box and press **Save preset**.
5. Another day — another run of Typhon, the same project: **Connect**, pick
   **Bird bath** in the preset box. The biases, filters and the camera's ROI
   are back; press **Start capture**.

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
