# PLC trigger (Do-More): taking a picture of every bottle

The **PLC trigger (Do-More)** plugin connects Typhon to an AutomationDirect
Do-More PLC (BRX, H2-DM1E or T1H-DM1E, programmed in Do-More Designer). A
photo-eye on the conveyor tells the PLC when a bottle passes. The plugin
makes sure the camera takes the picture when the bottle is in the middle of
the view, allowing for the belt speed and the delays along the way.

It is a built-in plugin that is **off** in a new project. To turn it on, open
**Plugins…** in Typhon (plugins), tick **PLC trigger (Do-More)**, and press
its button on the toolbar strip to open its window.

---

## 1. Pick a mode

| | **Wire** (recommended) | **Network** |
|---|---|---|
| What fires the camera | A PLC output wired to the camera's trigger input (a Basler `Line1`, or `CxpTrigger0` through the frame grabber) | Typhon, by software trigger, when the PLC's bottle count goes up |
| Timing | Microseconds: the camera waits its own **TriggerDelay** | A few milliseconds of spread (one poll plus Windows scheduling) |
| Wiring | One output to the camera or grabber | None; just Ethernet |
| Good for | Production, fast belts | Slow belts, bench tests, trying things out |
| Typhon's Triggering window | Mode **Triggered: exact**, Trigger from **Line1** / **CxpTrigger0** | Trigger from **PLC (network)**, mode exact, snapshot or record |

Both modes use Modbus TCP to read the bottle count and the belt speed. The
window's **Mode** box picks one. The **PLC (network)** source appears under
Trigger from only in network mode.

A third wiring also works in wire mode: wire the photo-eye **straight to the
camera's input** and let the PLC only count bottles. That removes the PLC's
scan time from the latency.

---

## 2. How the timing is worked out

```
travel        = photo-eye-to-view distance / belt speed
camera delay  = travel - fixed latency + calibration offset      (wire)
fire at       = crossing + travel - fixed latency + offset       (network)
```

- **Photo-eye to view** is measured along the belt, from the photo-eye's
  beam to the middle of the camera's view.
- **Fixed latency** is the sum of the delays that don't depend on speed: the
  PLC's scan time, the output switching on, and the camera's own trigger
  latency. Start at 0, or about 1 to 2 ms for a PLC in the loop, and let
  calibration correct it.
- **Calibration offset** is what auto-calibration adds. A positive value
  fires later.
- In **network mode**, Typhon only sees the count change when it next reads
  it. It assumes the bottle crossed half a poll and half a network round trip
  earlier.

In wire mode, the window shows the delay needed and the delay the camera
has. **Apply delay to camera** sets it on the camera shown in the main
window (Typhon's `capture.delay_us`, which is the camera's **TriggerDelay**).
With **Keep the camera's delay matched** ticked, Typhon sets it again
**between runs** whenever the speed changes. During a run the delay stays
fixed, because the camera's trigger is armed. If the belt speed changes a lot
during runs, see "Speed that changes during a run" below.

Cameras limit how long TriggerDelay can be (many Baslers allow up to 1 s).
If the delay you need is longer, the camera clamps it and the window says
what it took. In that case, move the photo-eye closer to the camera.

---

## 3. Set up the PLC in Do-More Designer

The names below are Do-More Designer's. Menus can differ a little between
versions.

### 3.1 Turn on the Modbus TCP server

1. Open **System Configuration** and the internal Ethernet port's settings
   (`@IntEthernet` on a BRX).
2. Tick **Enable Modbus/TCP Server**. The port is 502.
3. Give the PLC a fixed IP address on the camera PC's network, for example
   `192.168.1.10`, and write the program to the PLC.

Do-More's Modbus server shows its **MHR** block as holding registers:
`MHR1` is Modbus `40001`, `MHR2` is `40002`, and so on. Its **MIR** block
shows as input registers: `MIR1` is `30001`. Typhon accepts either spelling.

### 3.2 The registers Typhon reads

| Register (default) | What the ladder keeps there | Typhon setting |
|---|---|---|
| `MHR1` | Bottle count: add 1 per bottle (wrapping past 65535 is fine) | **Bottle count** |
| `MHR2` | Belt speed in mm/s (or tenths: set **units per mm/s** to 10) | **Speed register** (when **Belt speed** is *From PLC register*) |

If the belt always runs at one speed, choose **Belt speed: Fixed**, type in
the speed, and leave `MHR2` out of the ladder.

### 3.3 Ladder: count bottles (both modes)

```
Rung 1   STRPD X0                 (photo-eye: on its rising edge)
         INC   MHR1               (one more bottle)
```

### 3.4 Ladder: fire the camera (wire mode only)

Use a **transistor (DC) output**, not a relay: a relay adds several
milliseconds and bounces. Give the camera a clean pulse of a few
milliseconds:

```
Rung 2   STRPD X0                 SET Y0          (output on at the bottle)
Rung 3   STR   Y0                 TMR T0  5 ms    (time the pulse)
Rung 4   STR   T0.Done            RST Y0          (output off)
```

Wire `Y0` to the camera's opto-isolated trigger input (a Basler's `Line1`)
or to the frame grabber's trigger input that forwards `CxpTrigger0`. Check
the camera's or grabber's manual for the voltage and polarity it needs. Set
the edge (**RisingEdge**) and, if the line is noisy, a debounce in Typhon's
Triggering window.

### 3.5 Belt speed (optional)

If there is an encoder, have the PLC turn its count rate into mm/s and keep
it in `MHR2`. On a BRX, use its high-speed counter's rate, times mm per
count. Without an encoder, use **Belt speed: Fixed**.

---

## 4. Set up Typhon

1. In the plugin's window, type the PLC's **Address**, pick the **Mode**,
   and fill in **Photo-eye to view**, **Belt speed** and the registers.
2. Press **Test connection**. It should say *The PLC answered* with the
   count and speed.
3. **Wire mode:** in the Triggering window, choose **Triggered: exact** from
   **Line1** (or **CxpTrigger0**), then press **Apply delay to camera**.
   **Network mode:** choose Trigger from **PLC (network)**.
4. Start a run, let a few bottles through, and press Stop.

To try it without hardware, tick **Use a simulated PLC**. A pretend PLC and
belt run on this PC at the fixed speed you typed, one bottle per 200 mm.

---

## 5. Auto-calibrate

After a short run with bottles in the pictures:

1. Set **View along belt**: how many mm of belt one picture spans along
   the belt's direction. Set **Bottles move** to the belt's direction in the
   picture.
2. Press **Calibrate from newest pictures**. Typhon finds the bottle in the
   newest pictures (10 by default) by its edges. It measures how far the
   bottle sits from the middle and turns that into milliseconds at the belt
   speed. It then corrects **Calibration offset**.
3. Press **Apply delay to camera** (wire mode), run again, and calibrate
   again if needed. Each pass should bring the bottles closer to the middle.

Calibration works best on a plain belt. A picture where no bottle stands out
is skipped, and the result says so. You can always type the offset by hand.

---

## 6. The missed-bottle check

When the plugin is on and the PLC can be read, every run records the PLC's
count at Start and again at the end. The run's end record
(`<run>_run_end.json`, under `plugins` → `typhon.plc`) then holds:

- `bottles`: how many bottles the PLC counted during the run;
- `frames_saved` per camera, and `missed` (frames short of bottles × frames
  per trigger);
- in network mode, `network.triggers` (how many Typhon fired), `late`
  (bottles fired late, after the time they were due) and `merged` (bottles
  that arrived between two reads);
- the speed at Start and at the end, and the delay that speed made.

If any frames are short, the status line says so.

---

## 7. Speed that changes during a run

During a run, the camera's delay is fixed by the speed at Start. If the belt
speeds up or slows down a lot during runs, you can:

- use **network mode**, which reads the speed every quarter second and times
  each bottle with it; or
- keep the wire, and have the PLC delay its output by **encoder counts**
  instead of time. Fire `Y0` a set number of counts after the photo-eye,
  then set Typhon's distance to 0. The bottle is then always in the same
  place whatever the speed. (Typhon doesn't write that count to the PLC yet.
  It's a possible next step.)

---

## 8. When something is wrong

| What you see | What to do |
|---|---|
| *cannot reach the PLC at …* | Check the IP address and the cable, and that the PC is on the PLC's subnet. Try `ping`. |
| *the PLC refused: illegal function* | Modbus/TCP Server is not enabled (3.1). |
| *the PLC refused: illegal data address* | That MHR/MIR isn't in the Modbus map. Check the register name. |
| Bottle count never changes | The photo-eye input or the `INC` rung. Watch `MHR1` in Designer's Data View. |
| *camera has no setting called 'capture.delay_us'* | The camera shown has no hardware trigger delay (the simulated or an event camera). Show the Basler in the main window. |
| Bottles are always off-centre by the same amount | Calibrate (5), or adjust **Fixed latency**. |
| Bottles drift off-centre as the speed changes | Use *Belt speed: From PLC register*, and see 7. |

The plugin's code is in `typhon_plugins/plc/` (`modbus.py` is the Modbus
client, `timing.py` the sums and `simulator.py` the pretend PLC). The
picture check is `council_core/bottle_position.py`, which the plugin reaches
through `typhon_plugin_api.builtin` because it needs numpy and Pillow. Its tests are in
`tests/test_typhon_plugins_plc.py`.
