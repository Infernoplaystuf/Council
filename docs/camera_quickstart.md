# Running Barbie Capture v4 against a real camera

`barbie_capture_v4` is the Barbie GUI with a live camera driving it. This is
what to do on a machine that actually has one.

## What is verified, and what is not

**Basler / pypylon — verified.** The whole backend runs against real pypylon,
exercised end to end through pylon's camera emulator (`PYLON_CAMEMU`):
enumeration, opening, the GenICam node map, AOI, exposure, gain, grabbing,
Mono12 → uint16, and a lossless 16-bit PNG round trip. What the emulator
cannot prove is the CoaXPress transport itself and the boA5320's own node set.

**Prophesee / Metavision — NOT verified.** The SDK is not on PyPI and was not
installed here. The backend was rewritten against the published API after an
audit found the first version could not have worked (it pulled events through
a method that does not exist, and would have shown a healthy EVK4 as silent
for ever). Treat it as untested code that reads correctly, not as a working
driver.

## 1. Get the branch

```bash
git clone <your remote> Council-Demo
cd Council-Demo
git checkout qt-migration
```

## 2. Make an environment with the camera SDK in it

The app runs under whichever Python you point it at, so the SDK does not have
to live in the Council's own environment.

```bash
conda create -n pylon python=3.11 -y
conda activate pylon
pip install pypylon PySide6 numpy pillow scikit-learn
```

### CoaXPress cameras need more than the pip wheel

A **boA5320-150cm** is boost series on CXP-12. `pip install pypylon` alone will
enumerate **nothing**: since pypylon 4.0.0 the CXP GenTL producer was dropped
from the Windows wheel.

1. Install the **pylon Software Suite** from Basler with the
   **"CXP Camera Support"** feature ticked.
2. In the pylon Viewer, check the interface card's **InterfaceApplet** matches
   how many CXP cables the camera actually uses
   (`Acq_SingleCXP12Area` for one, `Acq_DualCXP12Area` for two, …). The wrong
   applet degrades or breaks the camera.
3. Confirm the camera appears in the pylon Viewer before trying this app, and
   then **close the Viewer** — the grabber channel is exclusive per process.

If the camera list is empty, read the "Backends not searched" panel: it names
exactly what is missing rather than making you guess.

## 3. Build the app for Qt

```bash
python run_example_gui.py barbie_capture_v4 --target qt --python pylon --no-run
```

`--target qt` is required. The live view uses the Qt `ImageCanvas.set_array`
path, which takes a numpy frame straight to a `QImage`; the Tk target has no
equivalent. `--python pylon` records that environment in the project's
manifest, so the GUI Designer's **Run** uses it afterwards too.

The command prints the project directory and the exact line to run it with.

## 4. Turn the live view on (one line, once)

`app.py` is generated once and never rewritten, which is where behaviour
belongs. Add one line to `App.__init__`:

```python
class App(HandlerMixin, MainUi):
    def __init__(self, parent=None, **kw):
        super().__init__(parent)
        import frame_camera
        frame_camera.attach(self)          # <-- this
```

Or patch it without opening an editor — replace `<project>` with the directory
the build printed:

```bash
python -c "import sys,io; p=sys.argv[1]+'/app.py'; s=io.open(p,encoding='utf-8').read(); s=s.replace('        super().__init__(parent)','        super().__init__(parent)\n        import frame_camera\n        frame_camera.attach(self)',1); io.open(p,'w',encoding='utf-8').write(s); print('wired')" "<project>"
```

`attach` starts a 33 ms timer on the UI thread that pulls the newest frame.
That direction matters: the grab thread never touches a widget, which is what
makes this legal in a generated Qt app at all.

## 5. Run it

```bash
python "<project>/main.py"
```

## Using it

1. **Scan for cameras** — the list fills; anything that could not be searched
   is explained in the panel on the right.
2. Select a camera, **Connect** — the status line reports the sensor size.
3. Pick a **capture folder** with the folder picker at the top left.
4. **Start capture** — the live view runs and frames are written to that
   folder as PNGs.

The capture folder *is* the browse folder, so the scrubber, **Scan for bad
timings** and the whole classifier panel work on what you just captured, with
no export step.

### The area of interest

Type `x, y, w, h` into the ROI box, or drag a rectangle on the live view, then
**Apply area to camera**. This sets the camera's **own AOI** — the sensor reads
out less, which is where the frame rate comes from — so frames arrive at that
size and are saved at that size. The value is snapped to what the sensor
accepts and the status line says so when it had to move. **Full sensor** puts
it back.

### Reading the status line

`60.0 fps · 242 grabbed · 157 dropped · 242 saved`

- **dropped** counts frames the *display* never showed. That is normal and
  deliberate — the screen consumes about thirty a second whatever the camera
  does — and it is shown so the number you read is never a lie about what the
  camera did.
- **saved** never drops. If a write fails, recording stops and says so,
  because a capture with a silent hole in it is worse than one that ended.

Each run writes under its own timestamped stem, so starting a second run into
the same folder adds to it and can never overwrite the first.

## Pixel depth

A Mono12 frame arrives as `uint16` holding 0–4095 and is saved as a 16-bit PNG
with those values **unaltered** (verified lossless). Nothing rescales your
pixels to make the picture look better.

One known consequence: `frame_classes.thumbnail` scales 16-bit input by 1/256
assuming it fills the full range, so a 12-bit frame reads dark *to the
classifier*. That is a conversion bug in the reader, not a reason for the
writer to change your data.

## If something goes wrong

| What you see | What it means |
|---|---|
| Empty camera list | Read the "Backends not searched" panel. On CXP it is almost always the pylon Software Suite / CXP Camera Support step. |
| "could not open … something else is holding the frame grabber" | The pylon Viewer, or a previous run, still owns the card. Close it. |
| Camera connects, live view stays blank | Check the AOI — **Full sensor** resets it. |
| An EVK4 opens and reports silence | Expected: the Prophesee path is unverified. Please send the status line and any error. |
