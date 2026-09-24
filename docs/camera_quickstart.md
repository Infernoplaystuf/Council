# Running Barbie Capture / Typhon against a real camera

`barbie_capture_v5` is the Barbie GUI with a live camera driving it and a
setup wizard that runs the first time it opens. `typhon` is the same app under
another name, in `#045f80`. This is what to do on a machine with a camera.

## What is verified, and what is not

**Basler / pypylon — verified end to end** through pylon's camera emulator
(`PYLON_CAMEMU`): enumeration, opening, the node map, AOI, exposure, gain,
grabbing, Mono12 → uint16, and a lossless 16-bit PNG round trip — on numpy 1.x
and 2.x. The emulator cannot prove the CoaXPress transport itself or the
boA5320's own node set.

**Prophesee / Metavision — verified against a real Metavision build, not a
camera.** OpenEB was built from source and the backend driven through a
synthetic EVT2 recording: callback registration, decoding, event fields and
coordinates, end of stream. Still unproven: enumerating and opening a live
USB EVK4.

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

1. **Scan for cameras**, select one, **Connect** — the status line reports the
   sensor size.
2. Pick a **capture folder** with the folder picker at the top left.
3. **Start capture** — the live view runs and frames are written to that folder
   as PNGs.

The capture folder *is* the browse folder, so the scrubber, **Scan for bad
timings** and the classifier all work on what you just captured.

### The area of interest (Basler)

Type `x, y, w, h` into the ROI box, or drag a rectangle on the live view, then
**Apply area to camera**. This sets the camera's **own AOI**, so frames arrive
at that size and are saved at that size. It is snapped to what the sensor
accepts, and the status line says so when it had to move. **Full sensor** puts
it back.

### Reading the status line

`60.0 fps · 242 grabbed · 157 dropped · 242 saved`

- **dropped** counts frames the *display* never showed — normal and deliberate,
  and shown so the number is never a lie about what the camera did.
- **saved** never drops. If a write fails, recording stops and says so.

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
| EVK4: empty list with the camera plugged in | The USB driver, almost always (Prophesee step 3). The SDK cannot tell "no driver" from "no camera" — both are an empty list. |
| EVK4: "Metavision SDK installed" fails | Not installed, or installed for a different Python than the app runs under (3.10–3.12 only). |
| Camera connects, live view stays blank | Check the AOI — **Full sensor** resets it. |
