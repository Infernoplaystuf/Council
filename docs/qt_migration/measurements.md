# Tk -> Qt migration: MEASURED FACTS (Council / "Data's Inferno")

Everything here was measured on the user's machine (Windows 11, 1920x1080 at 125%
scaling) or read out of the repo on 2026-09-11..15. Treat it as ground truth and
do NOT re-derive it. Where a number is an estimate it says so.

Worktree under analysis:
C:\Users\apkun\Downloads\Council-Demo\Council-Demo\.claude\worktrees\priceless-vaughan-cc9023
Branch: qt-migration, based on Work-Build @ 094c84d (unchanged as of today).

## 1. Repo state and in-flight work (rebase surface)

Two other efforts are UNCOMMITTED on the same base 094c84d:
* worktree jolly-zhukovsky-1158fd -- "emit the 11 GUI props the designer drops":
  modifies gui_shapes.py (+112/-13, prop schemas, RETIRED_PROPS), gui_emit.py
  (+321 lines, across construct(), emit_main_ui(), and the ImageCanvas block of
  WIDGETS_PY), gui_canvas.py (22), gui_examples.py (8), plus a new
  tests/test_gui_props.py (sets each prop to a non-default value and fails if the
  generated code does not change).
* main checkout -- ml_forge / label_check: gui_emit.py LINKED_ALLOWLIST and
  gui_policy.py LINKED_MODULES both gain "ml_forge", "label_check"; untracked
  ml_forge.py, label_check.py, examples/gui/label_check.gspec,
  tests/test_label_check.py.
CONSEQUENCE: any Qt work that edits gui_emit.py heavily will conflict. There is
no ml_forge.gspec (only label_check.gspec, untracked), so the Phase 2 slice
target is examples/gui/barbie_capture_v2.gspec.

## 2. Tk inventory -- council_gui_engine.py

22,610 lines total (20,672 non-blank). class CouncilConsole(tk.Tk), ~407 methods,
lines 5457-22415. Module level 1-5456 contains NO Tk calls.

Widgets constructed: ttk.Label 208, ttk.Button 194, ttk.Frame 189, ttk.Entry 38,
ttk.LabelFrame 30, ttk.Checkbutton 25, ttk.Combobox 21, ttk.Scrollbar 16,
ttk.Separator 16, ttk.Treeview 10, ttk.Notebook 5, ttk.Spinbox 4,
ttk.Radiobutton 4, ttk.PanedWindow 1; tk.Toplevel 20, tk.Listbox 17, tk.Text 10
(+39 more via the _make_text helper at 6039), tk.PanedWindow 10, tk.Label 5,
tk.Canvas 2 (scroll containers only), tk.Frame 1, tk.Button 1.
tkinterweb.HtmlFrame 1.
ZERO: Menu/menubar, Scale, Progressbar, ScrolledText, OptionMenu, Message.

Geometry: .pack( 748, .grid( 50 (in only 5 methods), .place( 0,
columnconfigure 1, rowconfigure 0, pack_forget 8.

Theming: ONE ttk.Style(self) at 6010 inside _apply_dark_theme (5992-6037),
theme_use("clam"), 15 style.configure + 3 style.map, NO named styles (only stock
class names). Palette in branding.py (DARK_THEME:76, LIGHT_THEME:94, THEMES:111,
get_theme:117). No light mode is wired up -- 5998 hard-codes get_theme("dark")
and self._theme is never read again. 271 hex colour literals (36 distinct), 187
lines set fg/foreground, 66 set bg/background, 71 font=(...) tuples (27 default
family, 25 Consolas, 19 Segoe UI). ROLE_COLORS 5147-5162 feeds transcript tags.

Variables: StringVar 83, BooleanVar 25, IntVar 3, DoubleVar 0; textvariable= 83
lines, variable= ~29; trace_add 3; legacy .trace( 0.

Event loop: .after( 111 lines (83 of them after(0, ...)), after_cancel 2,
after_idle 0, update_idletasks 3, update() 1, mainloop 1, wait_window 0,
grab_set 2. Recurring timers: _poll_ui_queue every 50 ms, log flush 30 s, node
probe 15 s, title refresh, grapher live reload.

THREADING -> UI (the important part). 61 Thread(...) sites, all daemon. One
queue.Queue (self.ui_q, 5556) with 99 .put calls over 29 message kinds, drained
by _poll_ui_queue (20902-21227) -- an if/elif dispatcher with 36 branches that
reschedules itself every 50 ms. That is the SAFE path. Three unsafe paths exist
today:
  (a) self.after(0, fn) called FROM worker threads -- most of the 83 after(0)
      sites (e.g. 6755-6787 HF download, 10846-11051 NX, 12547-12789 Models,
      16107-16262 scraper, 16474-16557 index builds, 21640-21664, 22477). It is
      also handed out as call_soon=lambda fn: self.after(0, fn) to gui_runner
      (11623). A comment at 12508-12512 records real "main thread is not in main
      loop" errors from this.
  (b) Direct Tk access from workers: Tk variable .set("") at 17229-17230, 17315,
      17346; .get() in TTS workers at 22373, 22402; _append_transcript (a Text
      insert, defined 18207, no thread guard) called from a worker at
      22125/22129.
  (c) No event_generate, no after_idle, no main-thread assertions anywhere.

Bindings: .bind( 38, bind_all 8 (Ctrl+= / Ctrl+- / Ctrl+0 UI zoom at 5512-5517),
unbind_all 1, bind_class 0, tag_bind 6, protocol("WM_DELETE_WINDOW") 1 (5581).
Mouse wheel: ONE handler (13660-13667) using ev.delta // 120 -- no Button-4/5
branch, no platform check. The 12 sys.platform checks are about opening files,
not input.

Dialogs: messagebox 55 (showinfo 15, showerror 14, askyesno 14, showwarning 10,
askyesnocancel 2), filedialog 11, simpledialog.askstring 7, plus 20 hand-built
tk.Toplevel dialogs.

Images/charts: the engine itself uses NO ImageTk, PhotoImage, FigureCanvasTkAgg
or Canvas create_* drawing. Charts live in plots_pane.py (FigureCanvasTkAgg +
NavigationToolbar2Tk at 166-195, ImageTk thumbnails at 225); the window icon
PhotoImage is in branding.py:241. Plotly output opens in a browser.

Tabs: 14 default (Council, Dream3D, Grapher, Specialists, Models, Lens, Sessions,
Vault, Agent Jobs, Tool Creation, GUI Designer, Speech, Changelog, Diagnostics)
+ 6 advanced-mode (IDE/Runner, Librarian, Agents, Nodes, Vault Health,
Apothecary). Delegated panels: plots_pane + tkinterweb (Grapher),
gui_canvas.DesignerCanvas + gui_wizard (GUI Designer), sage_agent.SageTuningPanel
+ vault_agent.VaultAgentPanel (Agents), apothecary_engine.ApothecaryConsole
(Apothecary, fully delegated), db_connect_wizard (Vault).

Tk-specific APIs: winfo_* only 5 calls; tk.call only 3, all "tk scaling"
(5500, 5504, 18168) -- the ONLY DPI handling in the app; no SetProcessDpiAwareness
anywhere. branding.apply_window_icon x5 (iconbitmap + iconphoto + ctypes
WM_SETICON). clipboard 4 clear/append pairs. No font measuring. Text rich text:
tag_configure/tag_config 31 (24 Text, 7 Treeview), tag_bind clickable chips
13179-13250, "1.0" index 77 times, see("end") 13, state normal/disabled toggling
on 117 lines, no marks, no Text.search. Treeview: heading( 24, column( 24,
tags= 3. Listbox curselection 23. tk.TclError caught 12 times.
crash_reporter.install_tk_hook replaces report_callback_exception (22490).

Split estimate (regex-based, rough): ~6,800 lines neutral (~30%), ~4,200 lines of
logic whose only UI contact is an output call (~18%), ~11,650 Tk-bound (~52%).

## 3. Tk inventory -- the other 21 modules

NEVER LOADED by the running app (dead): agent_panel.py (419), system_panel.py
(275), grapher_app.py (1460), tab_grapher.py (93), council_modules.py (220),
phase1_ai_model_council.py (815). gui_snap.py has no production importer either
(gui_canvas has its own snapping). tab_grapher.py:88 imports
grapher_app.run_standalone, which does not exist.

gui_canvas.py (1972) -- the Designer canvas, embedded at council_gui_engine:11301.
  Tk-free: undo, snapping, align/distribute, shape_at, handle_at, resize_box,
  containment_map (63-327, ~265 lines) + 36 display-free tests.
  Widget 333-947 (~615), 27 per-kind renderers 949-1593 (~645), _Inspector
  1600-1954 (~355).
  Four modes (draw/move/resize/band). 7 mouse + 16 key bindings. Snapping is
  built in (grid 8, edge threshold 6, sibling edges/centres, dashed guides).
  Undo = deepcopy of the whole shape list, depth 60. NO zoom.
  redraw() calls canvas.delete("all") and repaints EVERYTHING on every mouse
  motion; it keeps no canvas items, uses no find_overlapping/itemconfig/coords/
  move/tag_bind; hit-testing is plain Python. All 27 renderers draw through 5
  wrappers: _r (~43 calls), _ln (~22), _tx (~27), _poly (~7), _oval (~2).
  Interaction tests call private _press/_drag/_release with fake events carrying
  only .x/.y/.x_root/.y_root.
python_envs.py (461) -- does NOT import tkinter in-process. The "import tkinter"
  at :176 is TEXT inside the _PROBE script that runs in the TARGET interpreter;
  probe() refuses an interpreter with no tkinter ("it has no tkinter: ",
  :300-304).
splash.py (453) -- overrideredirect frameless window, tk.Canvas polygons, 33 ms
  animation loop, Toplevel of the main root. Skipped under COUNCIL_NO_SPLASH=1.
activation_dialog.py (309) -- modal Toplevel, grab_set, refuses WM_DELETE_WINDOW;
  licensing network calls block the UI thread. DEMO_MODE is on by default so the
  startup gate usually does not fire.
crash_reporter.py (338) -- lazy Tk import inside show_dialog (:191); installs
  sys.excepthook/threading.excepthook before any root exists, and
  root.report_callback_exception. BUG at :297: calls tk.messagebox.showerror
  without importing tkinter.messagebox -- works only because another module did.
branding.py (257) -- apply_window_icon lazily imports tkinter, sets the icon three
  ways including ctypes LoadImageW/SendMessageW(WM_SETICON) via wm_frame().
apothecary_engine.py (1861) -- ~910 neutral, ~950 Tk. WORST threading in the repo:
  after(0) from worker/monitor threads AND direct Text widget writes from a
  worker (_dlog at :1144 called from the thread at :1177, :1185, :1195, :1206).
  Its ui_queue parameter is stored (:945) and never used.
onboarding.py (1259) -- first-run wizard; grab_set, parent.wait_window nested loop
  (:1257); canvas.bind_all("<MouseWheel>") at :814 is NEVER unbound, so the
  binding outlives the wizard; a worker thread imports council_gui_engine (:1085).
vault_agent.py (770) -- ~556 neutral; worker calls self.after(0, ...) directly.
sage_agent.py (632) -- ~356 neutral; 6 messageboxes with no parent=.
plots_pane.py (259) -- FigureCanvasTkAgg + NavigationToolbar2Tk + ImageTk
  thumbnails; bind_all/unbind_all MouseWheel on Enter/Leave;
  matplotlib.use("Agg") at import (:36) changes the backend process-wide.
gui_wizard.py (429), db_connect_wizard.py (424), gui_runwith.py (77) -- small,
  mostly Tk, opened as Toplevels from the Designer / Vault tabs.
council_gui_engine.py BUG: self.root is used at 14765, 14847, 14858 but never
assigned (CouncilConsole IS the root) -- those paths raise AttributeError.

## 4. Tests

tests/conftest.py has a SESSION-scoped tk_root fixture (one root per session,
withdrawn). Its docstring records why: on Python 3.11 / Tcl 8.6.15, creating a
root after destroying one fails with "Can't find a usable init.tcl", and with one
root per file that failure silently hit the skip branch so 13 tests never ran.
Live-Tk tests: 8 files / 55 tests -- test_gui_canvas_interaction 17,
test_gui_wizard 14, test_gui_roi 10, test_gui_sequence 9, test_gui_errors 2,
test_frame_classes 1, test_gui_requires 1, test_python_envs 1.
test_gui_canvas.py (36 tests) needs no display. smoke_test.py:4047-4053 creates
its own root. 10 test files ASSERT that a module does not import tkinter
(gui_emit, gui_colors, gui_layout, gui_ports, gui_examples, gui_projects,
gui_shapes, gui_snap, gui_spec, gui_templates).

## 5. MEASURED: Tk vs Qt on this machine

Display: 1920x1080 at 125% => 1536x864 logical.

Tk (council env, Python 3.11.14, Tcl/Tk 8.6.15):
  process DPI awareness 0 (UNAWARE); GetDpiForSystem reports 96; screen 1536x864;
  tk scaling 1.3346 px/pt; winfo_fpixels("1i") 96.09; ttk theme "vista".
  TkDefaultFont/TkTextFont/TkHeadingFont/TkMenuFont = Segoe UI 9;
  TkFixedFont = Courier New 10.
  Arial 10: "The quick brown fox" = 115 px wide, linespace 16.
  ttk.Button("Run") 76x25, ttk.Entry 126x21, tk.Button("Run") 32x26.
  A 400 px-wide Tk window measures 520 PHYSICAL px -- Windows bitmap-stretches it
  (this is why the Tk UI looks soft at 125%).

Qt (mechanicus env, Python 3.12.13, PySide6 6.10.2 / Qt 6.10.2, conda-forge):
  QApplication sets the process to PER-MONITOR AWARE V2; devicePixelRatio 1.25;
  logical geometry 1536x864 (SAME logical coordinate space as unaware Tk);
  logicalDotsPerInch 96, physical 110.1; style "windows11";
  HighDpiScaleFactorRoundingPolicy.PassThrough.
  Application font Segoe UI 9.0 pt (pixelSize 12); fixed font Courier New 9.
  Arial 10: width 116, height 15, lineSpacing 15 (vs Tk 115 / 16).
  QPushButton("Run").sizeHint 81x26, QLineEdit.sizeHint 110x26
  (vs ttk.Button 76x25, ttk.Entry 126x21 -- entry heights differ by 5 px).
  => .gspec pixel geometry maps 1:1 into Qt logical pixels; the app keeps its
     apparent size and becomes crisp instead of bitmap-stretched.

Live frame display, 1024x768, 120 frames, same machine:
  Tk  numpy -> PIL.Image -> ImageTk.PhotoImage -> Canvas.itemconfig ->
      update_idletasks: 59.2 fps grayscale, 52.0 fps RGB.
  Qt  numpy -> QImage (zero-copy wrap) -> QPixmap -> QLabel.setPixmap -> repaint,
      rendering at DPR 1.25 (i.e. MORE physical pixels):
      194.1 fps grayscale, 176.6 fps RGB.
  => ~3.3x, and the Qt path needs no PIL at all.

matplotlib: QtAgg imports and renders fine offline (backend "QtAgg", binding
  PySide6) with ZERO socket connect attempts recorded; TkAgg likewise zero.

Qt runtime actually loaded by a live QtWidgets+QtAgg app: 14 modules, 75.0 MB,
  of which ICU is 40.1 MB (conda-forge links Qt against ICU; the official pip
  wheels do not ship ICU on Windows).

HYBRID (Tk and Qt in ONE process) -- measured:
  Tk alone:            awareness 0, a 400 px window = 520 physical px.
  After QApplication() with the "windows" plugin: awareness becomes 2, the
    ALREADY-OPEN root shrinks to 416 physical px and a new Toplevel opens at 418
    -- i.e. the whole Tk UI shrinks ~20% the instant Qt initialises, while Tk's
    own metrics stay at 96 dpi / 1536.
  With QT_QPA_PLATFORM=offscreen: awareness stays 0, both windows stay 520,
    Qt DPR 1.0 -- so Tk and Qt CAN share one process safely in headless tests.

## 6. MEASURED: size and packaging

PyPI wheels (cp39-abi3-win_amd64, requires_python >=3.9,<3.15 so 3.11 is fine):
  pyside6_essentials-6.10.2 = 74,546,490 bytes (74.5 MB)
  pyside6_addons-6.10.2    = 164,712,775 bytes (164.7 MB)  <- NOT needed
  shiboken6-6.10.2         =   1,222,052 bytes
PySide6-Essentials contains: QtCore, QtGui, QtWidgets, QtHelp, QtNetwork,
  QtConcurrent, QtDBus, QtDesigner, QtOpenGL, QtOpenGLWidgets, QtPrintSupport,
  QtQml, QtQuick, QtQuickControls2, QtQuickTest, QtQuickWidgets, QtXml, QtTest,
  QtSql, QtSvg, QtSvgWidgets, QtUiTools.
  (So QtNetwork, QtQml/QtQuick, QtSql, QtDBus ARE present and must be denied by
  the policy gate, not assumed absent.)

PyInstaller, MEASURED (PyInstaller 6.19 borrowed from the nxpython env, run on
mechanicus' Python 3.12.13, --onedir, conda-forge packages, short build path to
dodge MAX_PATH):
  numpy + matplotlib(Agg), no GUI toolkit : 537.5 MB / 408 files
  + tkinter + TkAgg + PIL                 : 541.9 MB (+4.4 MB); Tk-only files
                                            12.1 MB across 927 files
  + PySide6 + QtAgg                       : 583.4 MB (+45.9 MB); Qt-only files
                                            53.6 MB across 133 files:
      root DLLs 31.5 MB (Qt6Gui 8.4, Qt6Widgets 6.2, Qt6Core 5.4, Qt6Network 1.7,
      Qt6Svg 0.6, pcre2-16 0.5, shiboken6 0.5, pyside6 0.2),
      PySide6 .pyd bindings 12.4 MB, translations 6.3 MB (96 files, removable),
      plugins 3.4 MB.
  Both built bundles launched and exited 0.
  ICU does not appear in the Qt delta because the conda baseline already had it;
  a pip-wheel build has no ICU at all. PyInstaller pulled QtNetwork in by itself.

council.spec today: line 167 EXCLUDES "PyQt5", "PyQt6", "PySide2", "PySide6"
  ("we use Tk, not Qt"); hiddenimports list tkinter submodules and
  matplotlib.backends.backend_tkagg; the bundle is ~1.3 GB on disk / ~600 MB
  zipped. .github/workflows/build.yml builds Work-Build-App on Python 3.11 and
  verifies `import tkinter` in its "Verify critical deps" step. PyInstaller's
  Splash feature (used in council.spec) is itself implemented with Tcl/Tk.

## 7. Environments on this machine

council     C:\Users\apkun\miniconda3\envs\council     Python 3.11.14 -- the
            project floor and the test env. matplotlib 3.10.8, numpy 2.3.5,
            pillow 12.0.0, pytest 9.0.2. NO PySide6, NO PyInstaller.
mechanicus  Python 3.12.13, PySide6 6.10.2 + qt6-main 6.10.2 (conda-forge),
            matplotlib 3.10.8, numpy 2.4.3, pillow 12.1.1. NO PyInstaller.
            Its numpy CRASHES (0xC06D007F, a delay-load fault; libblas.dll cannot
            resolve mkl_rt.dll) when python.exe is run WITHOUT conda activation.
            PySide6 and PIL import fine unactivated.
nxpython    Python 3.12.13, PyInstaller 6.19.0, qt6-main 6.9.3, no PySide6.
python_envs.py deliberately never uses `conda run` ("it buffers the child's
output and cannot be stopped cleanly") and calls an env's python.exe directly.
=> mechanicus cannot be a preview target for a numpy app as things stand.
Installing anything needs the user's explicit permission (it is a download).

## 8. The generated-app pipeline (what a Qt target must reproduce)

gui_shapes (.gspec, TOOLKIT-NEUTRAL) -> gui_layout -> gui_spec -> gui_ports (typed
ports IR) -> gui_emit (Tkinter source) -> gui_policy (AST gate) -> gui_runner
(preview process) -> python_envs (interpreter + self-check).

gui_emit writes, per project:
  ui/__init__.py, ui/widgets.py (WIDGETS_PY: ImageCanvas with pan/zoom-to-cursor/
  overlay/ROI, ChartPanel, Scrubber, LogPane, FilePicker, StatusBar, Toolbar --
  each with a "# region: custom:<id>" block), ui/ports.py (PORTS_RUNTIME +
  generated class Ports), ui/main_ui.py (MainUi + STOP_WATCHER), main.py
  (regenerated every time; carries the sys.path block and the `requires` check).
  app.py and handlers.py are created ONCE and NEVER rewritten.
Key contracts:
  * Regeneration is byte-identical for an unchanged spec (no timestamps).
  * MainUi: _build(), request_close(), on_close(), report_error(what, exc)
    (stderr + a dialog unless COUNCIL_NO_DIALOGS=1), clear_ports(*names).
  * STOP_WATCHER: only when COUNCIL_PREVIEW_CONTROL=stdin; a daemon reader thread
    watches raw fd 0 for "stop" or EOF, sets a flag, and a 150 ms ui.after poll
    calls request_close() -- the reader NEVER touches the toolkit. On EOF it
    redirects stdout/stderr to devnull first (an orphaned pipe made on_close
    raise on Windows).
  * gui_runner decides whether an app supports clean Stop by grepping
    ui/main_ui.py for the literal "_watch_for_stop(self)".
  * Ports API: .get/.set/.on_change/.enable/.widget/.clear, event ports
    .on_fire/.fire, Ports.read()/apply()/[name]/RENAMED aliases. Port classes:
    _VarPort (Tk variable), _TextPort, _ListPort, _TablePort, _ProxyPort
    (composite writer method), _TabPort, _EventPort. Plus _FrameBrowser (folder
    -> natural-sorted files -> index -> one decoded image, debounced 150 ms scan
    / 30 ms show, ROI two-way sync) and _natkey/_coerce/_PortsBase, which are
    toolkit-neutral apart from after()/after_cancel scheduling.
  * handler stubs (handlers.py) only touch self.ports.*, self.clear_ports and
    self.report_error -- they are ALREADY toolkit-neutral. app.py is NOT: it does
    `import tkinter as tk`, `root = tk.Tk()`, App(master).pack(...), mainloop().
  * PortSpec fields are neutral except var_class and tk_option (+PortCap.classic);
    a Qt emitter can work from kind/binder/type/direction/writer/deep.
  * gui_emit._CLASSIC_CLASS / gui_colors.COLOUR_CAPS exist because ttk ignores
    style background on the Windows "vista" theme (measured 0.0% vs clam 95.9%),
    so a coloured widget is emitted as a classic tk widget. Notebook, Treeview,
    Combobox and Progressbar cannot be coloured and the inspector refuses to
    offer a picker for them.
  * gui_policy: DENIED_MODULES (subprocess, socket, requests, urllib, http,
    ctypes, pickle, importlib, multiprocessing, builtins, ...), DENIED_ATTRS
    checked on EVERY ast.Attribute whether called or not, DENIED_BUILTINS,
    getattr(x, "literal") indirection, `from X import *` (only "tkinter" is in
    STAR_IMPORT_OK), THIRD_PARTY = {pandas, numpy, matplotlib, PIL, pillow},
    LINKED_MODULES, PROJECT_MODULES = {ui, app, handlers, launch, main, widgets,
    main_ui}, and `requires` widens the allowlist per project (check_requires
    refuses denied modules and Council code).
  * gui_runner is toolkit-neutral (Popen, stdin "stop", 5 s grace, then kill;
    call_soon marshals to the designer's Tk thread).

Example under consideration for the Phase 2 slice --
examples/gui/barbie_capture_v2.gspec:
  linked mode, requires numpy + PIL; kinds: label 11, file_picker 2, spinbox 3,
  image_canvas 1 (ROI), scrubber 1, button 2, entry 2, listbox 1; script links
  scan_report (frame_timing) and export_roi (frame_roi); one `drives` frame
  browser. barbie_capture_v3.gspec additionally needs sklearn and 9 script links.
  The linked modules frame_timing / frame_roi / frame_classes are pure Python
  (numpy + PIL) with no Tk, so they carry over unchanged.

## 9. Recommended binding (for the record)

PySide6 (LGPL, official Qt for Python) over PyQt6 (GPL or paid commercial),
because Data's Inferno is distributed with licensing.py/activation_dialog.py.
napari 0.9.1 (the user's label tool) needs Python >=3.11, depends on qtpy>=2.4.0,
and offers extras: pyside6 (PySide6 >6.7, !=6.11.0, !=6.11.1), pyqt6, pyqt5 --
but `napari[all]` installs PyQt6. Two different bindings cannot share one
process, so embedding napari requires that env to be built with napari[pyside6].
napari is NOT installed anywhere on this machine.
