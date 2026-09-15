# Tkinter -> PySide6: the migration plan

Status: **Phase 1 complete. Stage 1 (the vertical slice) is built and working** —
see §11 for what actually runs today. The numbers behind the plan are in
[qt_migration/measurements.md](qt_migration/measurements.md), and the scripts that
produce them are in [qt_migration/probes/](qt_migration/probes/).

Read section 9 first if you only read one part: it is the list of decisions that
are yours, not mine. They are still open — the slice was built so that answering
them is cheap, not to pre-empt them.

---

## 1. The recommendation in one paragraph

Use Qt where it pays and leave the shell alone. **Generated apps get a Qt target**
(they already run in their own process, so there is no event-loop or DPI conflict
with the Council, and the camera gap closes there), **Tk stays the default** until a
written parity checklist is green, and **`council_gui_engine.py` stays on Tk** for
now — porting it is a four-to-six-month job whose user-visible payoff is mostly
aesthetic, because the shell draws no images, no canvas graphics and no live frames.
The Designer canvas sits in between: it is a clean port (5-8 days) but it cannot
ship inside the Tk shell, because Tk and Qt cannot share a process (measured, §2),
so it has to wait for either a separate Qt process or the shell itself.

Total for the recommended scope (generated apps end to end, gate, envs, tests,
packaging): **roughly 35-55 engineering days**, of which a working vertical slice is
the first 6-8.

---

## 2. What was measured (not assumed)

All on this machine: Windows 11, 1920x1080 at 125%, Tk = council env (Python
3.11.14, Tcl/Tk 8.6.15), Qt = mechanicus env (PySide6 6.10.2 / Qt 6.10.2).

| Question | Answer |
|---|---|
| Does Qt change how the app looks? | Tk today runs **DPI-unaware** — Windows bitmap-stretches it, so a 400 px window is 520 physical px and text is soft. Qt is per-monitor-aware v2 and draws crisp in the **same logical space** (1536x864), so `.gspec` pixel geometry maps 1:1 and the app keeps its apparent size. |
| Fonts | Both default to Segoe UI 9. Arial 10 measures 115 px / linespace 16 in Tk, 116 / 15 in Qt. Widget metrics differ a little: `ttk.Entry` 126x21 vs `QLineEdit` 110x26. |
| Live frames (the camera case) | 1024x768, 120 frames: **Tk 59.2 fps** grayscale / 52.0 RGB (numpy->PIL->ImageTk->Canvas) vs **Qt 194.1 / 176.6** (numpy->QImage->QPixmap), with Qt rendering *more* physical pixels. ~3.3x, and the Qt path needs no PIL. |
| Can Tk and Qt share one process? | **No.** Creating a `QApplication` in a live Tk process flips the process to DPI-aware and the already-open Tk window shrinks from 520 to 416 physical px on the spot — Tk keeps reporting 96 dpi, so it cannot compensate. With `QT_QPA_PLATFORM=offscreen` nothing changes (520 -> 520), which is what makes a shared *pytest* process possible even though a shared *GUI* process is not. |
| matplotlib | QtAgg imports and renders offline with zero network attempts, binding PySide6. |
| Download size | `PySide6-Essentials` 74.5 MB + `shiboken6` 1.2 MB. `PySide6-Addons` (164.7 MB) is **not** needed. Wheels are cp39-abi3, so the 3.11 floor is fine. |
| Bundle size | Measured PyInstaller one-dir: baseline 537.5 MB, +Tk 541.9 (+4.4), +Qt 583.4 (**+45.9 MB**; Qt-only files 53.6 MB, of which 6.3 MB is removable translations). Both built bundles launched and exited 0. Caveat: that baseline is a conda build that already contained ICU; a pip-wheel 3.11 build will differ. |
| Does a generated Qt app pass our own policy gate? | **No, and the reason matters** — see §5. |

---

## 3. What is already toolkit-neutral (so it does not move)

- `.gspec` wireframes and `gui_shapes` — the palette, the prop schemas, the geometry.
- `gui_layout` -> `gui_spec` -> `gui_ports`: the IR is neutral apart from three
  Tk-named fields (`PortCap.var_class`, `tk_option`, `classic`) that a Qt backend
  simply ignores.
- `gui_runner` — the whole preview-process protocol (Popen, "stop" on stdin, EOF
  backstop, 5 s grace, then kill). Its one coupling is that it detects clean-Stop
  support by grepping `ui/main_ui.py` for the literal `_watch_for_stop(self)`, so a
  Qt `main_ui.py` must keep that marker.
- `handlers.py` — the generated handler stubs touch only `self.ports.*`,
  `self.clear_ports` and `self.report_error`. Already portable.
- The linked analysis modules (`frame_timing`, `frame_roi`, `frame_classes`,
  `ml_forge`, `label_check`) — pure Python, no Tk.
- Inside `council_gui_engine.py`, lines 1-5456 contain no Tk call at all, and about
  30% of the file overall is toolkit-neutral logic.

What is *not* neutral and decides the shape of everything: **`app.py` is created
once and never rewritten**, and it is the only file that builds the root window and
runs the loop. The toolkit is therefore a per-project, write-once property.

---

## 4. Recommended order

**Stage 0 — decisions and a test target (0.5 day + your call).**
Settle §9. If Qt tests are to run on the floor, create a `council-qt` env
(Python 3.11 + `PySide6-Essentials==6.10.2` + `shiboken6`); that is a ~76 MB
download and needs your say-so.

**Stage 1 — the vertical slice (6-8 days).** A Qt target for the emitter, good
enough to generate and run `examples/gui/barbie_capture_v2.gspec` from the same
`.gspec` as today: same ports API, same script links, same failure reporting, same
clean Stop, gate coverage, tests, screenshot. Acceptance criteria in §8.

**Stage 2 — parity for generated apps (12-20 days).** The remaining widget kinds and
composites (ImageCanvas ROI, ChartPanel, LogPane, Toolbar, StatusBar), the props
walk passing for both targets, the camera/thread API (§6), packaging and CI.

**Stage 3 — optional, independent of Qt: a de-Tk-ing pass on the shell (5-8 days).**
Worth doing *in Tk, now*, whatever you decide about Qt: one `call_soon` seam to
replace the ~83 worker-thread `after(0, ...)` sites and the six places a worker
touches a widget directly, semantic colour/font roles instead of 271 hard-coded hex
literals and 71 font tuples, and deleting 3,282 lines of dead modules. It fixes real
bugs today and halves the cost of any future port.

**Stage 4 — the Designer canvas (5-8 days), only once there is somewhere to put it.**
Clean port, but it cannot run inside the Tk shell.

**Stage 5 — the shell (79-123 days). Not recommended now.** See §7.

---

## 5. The policy gate — two findings, both verified by running it

Run `docs/qt_migration/probes/gate_probe.py` to reproduce.

**(a) `requires: PySide6` would open the whole of Qt.** The gate matches the
allowlist on the *root* module name (`gui_policy.py:266, 269`), and PySide6's root
covers every submodule. Declaring it — which `check_requires` accepts today with no
error — admits `QProcess`, `QtNetwork`, `QDesktopServices`, `QSettings`,
`QPluginLoader`, `QtQml` and `QtSql` in one step. So the Qt work must add a rule the
gate does not currently have: judge the **full dotted module name**, allow exactly
`QtCore`/`QtGui`/`QtWidgets`, and deny a set of names wherever they are spelled
(imported name, attribute, bare name, `getattr` literal). Note also that
`QtCore.QDir.removeRecursively` is Qt's spelling of `rmtree` and is not covered by
the existing destruction list, and that attribute traversal off the bare `PySide6`
package is another path in.

**(b) The gate would refuse correct Qt code.** `.load` is denied unless the receiver
is in `SAFE_LOAD_RECEIVERS`, so `QPixmap.load(path)` — the natural spelling — is
rejected. The emitter must use the constructor form `QPixmap(path)`; widening
`SAFE_LOAD_RECEIVERS` does **not** work, because the receiver there is a local
variable name. Everything else checked out clean: signals, lambdas,
`super().__init__`, `QImage` from a numpy buffer, `QFileDialog.getOpenFileName`,
`QTimer.singleShot` and `app.exec()` all pass. Do not "tidy up" by adding `exec` to
`DENIED_ATTRS` — it would refuse every Qt app.

**The interpreter check also blocks Qt today.** `python_envs` refuses any
interpreter without tkinter (`python_envs.py:300-304`), so a Qt-only env can never
be selected and Run always fails preflight. The probe has to become toolkit-aware.

---

## 6. Generated apps: what changes, what does not

**The ports API does not change.** `get/set/on_change/enable/widget/clear`,
`on_fire/fire`, `Ports.read/apply/[name]/RENAMED` all stay, so `handlers.py` keeps
working. Internally `_VarPort` (backed by a Tk variable) becomes a per-kind
read/write/signal adapter. Measured on PySide6 6.10.2: Qt's change signals **do**
fire on programmatic writes and do **not** re-fire on an unchanged value, so
`.on_change` keeps its Tk semantics; `setEnabled` cascades to children, so
`deep=True` becomes a documented no-op.

**Two honest divergences** to accept and document: a `QSpinBox` cannot show blank, so
`clear()` on a spinbox port keeps its value (joining checkbox/scale/scrubber in the
"no honest empty state" group); and `QSlider` is integer-only, so a float `scale`
needs a multiplier.

**Clean Stop survives unchanged in shape**: the reader thread still never touches the
toolkit, the 150 ms poll becomes a `QTimer`, the EOF stdout/stderr redirect stays,
and `_watch_for_stop(self)` stays in the file so `gui_runner` still detects support.
One real trap: Tk's `root.destroy()` ends the whole application, while Qt's
`close()` closes one window — the Qt version must quit the application explicitly or
a preview with a second window never exits.

**The camera API is the actual prize.** Generated Qt apps get a small documented
worker API (`QThread` + signals) that `handlers.py` can call, so a pypylon or
Metavision grab loop can push frames to the UI safely — plus the measured 3.3x frame
throughput and a zero-copy numpy->QImage path. To be honest about it: Tk *could* get
the same safety with a queue drained by `after()`, and that should ship for both
targets so `handlers.py` stays toolkit-neutral even when it drives a camera. What Tk
cannot match is the throughput and the DPI-correct display.

---

## 7. The shell: why not now

`council_gui_engine.py` is 22,610 lines, but only about 12% of it names a toolkit
API. The port is therefore "rewrite the presentation layer of a 17,000-line class
that has no UI tests at all" — 927 tests would stay green through a completely
broken window. The estimate is **79-123 days**, and my reviewers were right to call
the internal breakdown soft; treat it as "four to six months, with real uncertainty".

Three specific hazards if you ever do it:

- `QTimer.singleShot(0, fn)` called from a worker thread **never fires** — no
  exception, no log. That is the direct translation of the ~83 `self.after(0, ...)`
  sites, most of which are called from workers today. Stage 3 is the fix.
- The six places a worker already touches a widget directly are undefined behaviour
  in Qt too, and fail more destructively.
- The transcript's clickable chips (`tag_bind`) become HTML anchors in a
  `QTextBrowser`, which makes model output HTML-interpreted — `<` and `&` would
  render or vanish unless escaped.

The strongest argument for leaving it on Tk permanently: the shell displays text,
tables and charts; nothing in it benefits from Qt's rendering, and every one of its
20 tabs would have to be re-verified by hand.

---

## 8. Phase 2, if you approve it: the vertical slice

Build a PySide6 version of `examples/gui/barbie_capture_v2.gspec` from the **same**
`.gspec`. For reference, the Tk output of that example today is 1,780 lines of
`ui/` (`ports.py` 814, `widgets.py` 634, `main_ui.py` 331) plus `app.py` 39,
`handlers.py` 85, `main.py` 67, with 13 ports, and it re-emits byte-identically.

Acceptance criteria:

1. Generated from the unmodified `.gspec`; Tk output for every example stays
   **byte-identical** (golden files prove it).
2. Same ports API, same script links (`scan_report`, `export_roi`), same frame
   browser behaviour including the ROI two-way sync.
3. Failure reporting: cleared outputs plus a dialog, `COUNCIL_NO_DIALOGS=1`
   suppresses it, `clear_ports` never raises.
4. Clean Stop over stdin, verified by the existing `tests/test_gui_stop.py` pattern.
5. Passes the project's own policy gate, with the §5 rules in place.
6. Tests: emitter goldens, gate denials, a ports round-trip per binder, the props
   walk parametrised over both targets.
7. A screenshot of the running app.

Most of that test surface needs **no** PySide6 installed — the emitter is text and
the gate is AST. Only items 2-4 and the screenshot need a real Qt env.

---

## 9. Decisions that are yours

1. **Binding: PySide6, or PyQt6?** Recommend **PySide6** (LGPL; PyQt6 is GPL or paid,
   and Data's Inferno ships with licensing). Pin **6.10.2** — napari excludes
   6.11.0/6.11.1.
2. **May I install `PySide6-Essentials==6.10.2` (~76 MB) into a Python 3.11 env?**
   Recommend a **new `council-qt` env**, leaving `council` (the floor, and the "what
   a normal user has" env) without it. Without this, every Qt test skips itself and
   the work could be merged having never run on the floor — which my reviewers
   flagged as the single most likely way this goes wrong.
3. **Where does the per-project toolkit live?** My reviewers split three ways.
   Recommend: **`gui_projects.Manifest`** (keeps the `.gspec` toolkit-neutral and
   portable, as designed) **plus a guard that reads the toolkit back from `app.py`**
   and refuses to regenerate on a mismatch — which removes the one real objection to
   the manifest (an older build silently regenerating Tk `ui/` against a Qt `app.py`).
4. **Scope: do you want the shell ported at all?** Recommend **no, not now** — Qt for
   generated apps and new camera work; revisit the shell only if you hit something Tk
   genuinely cannot do.
5. **Does the shipped bundle carry Qt?** Recommend **yes** (+45.9 MB measured on a
   ~583 MB bundle) — otherwise nobody running the `.exe` can try a Qt project. Note
   PyInstaller's Splash is itself Tcl/Tk, so **Tk stays in the bundle regardless**.
   CI must also install PySide6; nothing in `build.yml` does today.
6. **Stage 3 (the de-Tk-ing pass) — ship it independently?** Recommend **yes**: it
   fixes real thread-safety bugs and 3 `AttributeError` paths, and deletes 3,282
   dead lines, whatever you decide about Qt.
7. **Is WSL/Linux in scope for Qt apps?** Every measurement here is Windows.
   Recommend saying explicitly that Qt generated apps are **untested on WSL/Linux**
   rather than leaving it silent.

---

## 10. Risks worth naming now

- **Rebase.** Two efforts are uncommitted on this same base: the "11 props" branch
  (+321 lines across `construct()`, `emit_main_ui()` and `WIDGETS_PY`) and
  ml_forge/label_check. The Qt target therefore goes in a **new `gui_emit_qt.py`**
  with a ~60-line dispatch hook in `gui_emit.py`, not a restructuring of it. Extract
  a `gui_emit_tk.py` later, as a move commit the goldens prove byte-for-byte.
- **A frozen Council that ships Qt can poison its own previews.** `gui_runner` copies
  `os.environ` wholesale into the preview child, and PyInstaller's PySide6 runtime
  hook sets `QT_PLUGIN_PATH` and prepends the bundle to `PATH` — so an external
  interpreter's Qt app would load the bundle's plugins. The Tk `TCL_LIBRARY` rthook
  already proves the channel exists. The preview environment must be scrubbed.
- **Test baseline.** `pytest tests` collects **927**; bare `pytest` collects **1103**
  — the extra 176 are in `inferno_local/tests`, which no area covered, and one of
  them creates and destroys its own Tk root in the way `conftest.py` documents as
  fatal. Decide which invocation is the contract before adding Qt tests.
- **Headless tests answer a different question.** Under `offscreen` the style is
  `fusion`, not `windows11`, and size hints differ. Anything about native look must
  be checked on screen, the same way the ttk vista/clam colour measurement was.
- **Two Qt bindings cannot share a process.** If a generated app's interpreter also
  has PyQt6 (e.g. `napari[all]`), it will crash. A five-line startup check that exits
  3 with a sentence is the cheap answer.
- **Text is reinterpreted by Qt.** `&` in a button caption becomes a mnemonic, and
  `QLabel` auto-detects rich text, so a caption like `<3 items` can vanish. The
  emitter must escape captions.

---

## 11. What is built (Stage 1, done)

The vertical slice from §8 exists and runs. `gui_emit.emit(spec, path, target="qt")`
writes a complete PySide6 project from an unmodified `.gspec`.

**Verified, not asserted:**

| Claim | How it was checked |
|---|---|
| Tk output did not move | Every example emitted through the new dispatch is **byte-identical** to the pre-change output, and `emit(...)` with no `target=` equals `target="tk"`. Pinned by `tests/test_gui_emit_qt.py`. |
| The Qt project runs | `image_viewer` generated as Qt, launched under PySide6 6.10.2: the frame browser found 8 frames, displayed `frame_1`, the scrubber moved to index 4 and the view followed to `frame_5`. Screenshot taken from the running window. |
| Clean Stop still works | Driven through the real `gui_runner`: `listens_for_stop=True`, the app **closed itself in 0.35 s with exit code 0**, `on_close` ran (where a camera releases its device), no kill needed. |
| A Qt project passes its own gate | `validate_dir(..., toolkit="qt")` is clean for all four examples; the same project is **refused** on a Tk target. |
| The escapes stay shut | 14 Qt escape spellings are refused, including the two the root-name allowlist used to miss: `from PySide6 import QtNetwork` and `import PySide6.QtNetwork`. |
| Ordinary Qt code is not refused | `app.exec()`, `QFileDialog.getOpenFileName`, `QTimer.singleShot`, `QPixmap(path)` all pass. `exec` must stay out of `DENIED_ATTRS` or every Qt app breaks. |
| Both targets agree where they must | Same wireframe -> identical `handlers.py` and identical port names on both targets. |
| Nothing regressed | Full suite green: 927 existing + 66 new. |

**New files:** `gui_emit_qt.py` (the Qt backend), `tests/test_gui_emit_qt.py`.
**Changed:** `gui_emit.py` (a ~60-line dispatch, nothing else), `gui_policy.py`
(the toolkit-aware allowlist and the Qt deny rules), `gui_projects.py`
(`Manifest.toolkit` + `toolkit_of()`).

**Design decisions taken while building, worth knowing:**

- **The toolkit is write-once per project, and `emit()` enforces it.** `app.py`
  runs the event loop and is never rewritten, so regenerating `ui/` for the other
  toolkit would leave a project whose `app.py` cannot drive its own UI. The
  truth is read back out of `app.py`, not trusted from the manifest.
- **Colour is a stylesheet scoped by `objectName`.** An unscoped one cascades into
  every child — measured: the whole app came out pink, entries and spinboxes
  included. Scoping also matches Tk, where `resolve_scene` has already decided
  each child's own colour.
- **`sticky` sets both alignment and size policy.** Qt takes stretch from the
  widget's size policy, and a `QPushButton` is Minimum/Fixed by default, so a
  cell Tk would have filled would otherwise hold a natural-height button.
- **Per-item padding rides in a nested layout.** `QGridLayout` has no per-item
  margins; `addLayout` rather than a wrapper widget keeps `parentWidget()` where
  the spec put it.
- **No `.load` anywhere in emitted code.** `QPixmap.load(path)` is the natural
  spelling and the gate refuses it; `QPixmap(path)` does the same job.

**Not done yet** (the rest of Stage 2): the designer UI for choosing the target,
the toolkit-aware `python_envs` probe (it still requires tkinter of every
interpreter, so a Qt project cannot yet be Run from the Designer against a
Qt-only env), the camera/thread worker API, and packaging. Grid-heavy layouts are
also under-exercised: all four examples are freeform designs, so `_place` carries
18-42 widgets each and `_cell` only one.
