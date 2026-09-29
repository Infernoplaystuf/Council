# Qt wireframe test cases

Ordinary wireframes, each built as a Qt project and opened by
`tests/test_qt_wireframes.py`: the .gspec is saved into a fresh vault project
created for Qt, the Designer's own Generate
(`council_core.designer_project.generate`) runs with no model, the policy gate
checks the result as Qt, every file compiles, and `App()` is constructed
offscreen, shown at 1100x700, and probed for what the line below promises.

**Tiers** are the file-name prefix: **A** uses kinds the Qt runtime was already
proven on; **B** uses what the Qt emitter fix made work; **C** is an edge where
Qt degrades by design, and the probe checks it degrades the way the generated
code says it does.

**These files are generated.** The builders live in
`tests/test_qt_wireframes.py`, which also checks every file against the
designer's rules (1100x700 canvas, 8 px grid, children 16 px inside their
container, no overlaps, one tab title per notebook page, whole-number bounds,
real lists, `#rrggbb` colours, no PySide6 in `requires`). Edit a builder, then
run `python -m tests.test_qt_wireframes --write`. A hand edit fails the drift
test.

**Known Qt loss (strict xfail)** on a line means the window is NOT yet what
was drawn, on Qt only, and `tests/test_qt_wireframes.py` pins exactly that
loss as a strict xfail (`DRAWN_SHARE`, `TOO_BIG`): the day it is fixed the run
fails until the entry comes out, and the line here must change with it — a
test holds the two to the same set of cases.

**Why a subfolder:** `gui_examples.names()` globs `examples/gui/*.gspec`
non-recursively, and what it returns can be sent to a designing model as a
worked example. These are fixtures, some degraded on purpose, so they stay out
of that glob.

| File | Project | Tier | Exercises | Expected |
|---|---|---|---|---|
| `a_login_form.gspec` | Login Form | A | labels, an entry, a password entry (`show="*"`), a button, a caption-less label bound as a port | builds and opens; entries read back through their ports, the password echoes as dots, Sign in reaches its handler once |
| `a_image_viewer.gspec` | Image Viewer | A | folder file picker -> scrubber `drives` {folder, target, current, status} -> image canvas; a status bar | picking a folder of 3 PNGs lists them in natural order, shows frame_1.png, and a scrubber step shows frame_2.png with "2 / 3" in the status bar |
| `a_list_editor.gspec` | List Editor | A | a listbox, an entry, three buttons, one labelled Remove (a gate-denied attribute name) | the listbox port sets and reads its items; Remove gets port `remove_button`, not `remove`; Add reaches its handler |
| `b_toolbar_editor.gspec` | Text Editor | B | a toolbar over a text area over a status bar, all full width | a toolbar button reaches `on_toolbar` with its label; the text and status ports write through |
| `b_split_browser.gspec` | Split Browser | B | a horizontal paned window holding a listbox and a three-column table | a QSplitter with both panes; the table's headers are the declared columns and its rows port round-trips |
| `b_options_group.gspec` | Options Panel | B | a label frame holding a three-radio group (same `group`, distinct `value`), two checkbuttons and a progress bar | the group box carries its title; the radio port starts at `small` and follows a click on Large; the progress port sets the bar; a checkbutton click reaches its handler |
| `b_notebook_tabs.gspec` | Tabbed Settings | B | a notebook whose three frame pages are drawn side by side, each holding controls; `tabs` names each page | three tabs titled General / Display / Advanced in drawing order, each page its frame; the tab port switches tabs; the combobox holds its values, the spinbox its range |
| `b_paned_vertical.gspec` | Console Split | B | a vertical paned window holding a text area and a log pane | a vertical QSplitter with both panes; the log port appends |
| `b_settings_form.gspec` | Settings Form | B | label/field rows: combobox (values), spinbox, scale, spinbox with increment 64; an Apply button | every field holds its declared values, range and step; Apply reaches its handler |
| `b_dashboard.gspec` | Dashboard | B | a chart panel, a log pane, a button and a checkbutton; `requires: ["matplotlib"]` (the one v3 case) | the declared package passes the gate; the chart panel draws a matplotlib figure; the log port appends |
| `b_nested_containers.gspec` | Nested Panels | B | containers two deep: a frame holding two label frames, each holding controls | every widget sits inside the group box it was drawn in, inside the outer frame; the two path entries are independent ports |
| `b_single_child_containers.gspec` | Single Child | B | a frame, a label frame and a freeform area, each holding exactly one widget (the old `QGridLayout.addLayout()` pack crash) | builds and opens; each sole child fills its container as Tk's `pack(fill="both", expand=True)` would, and the Preview frame, which gui_layout does not stretch, keeps at least 0.4 of its drawn 336x560 (measured 404x291; Tk 382x269). It rendered 66x66 until the Qt ImageCanvas asked for Tk's 378x265 instead of 40x40 |
| `b_single_root_shape.gspec` | Single Shape | B | one image canvas and nothing else (the root's own pack) | builds and opens; the canvas is the window's direct child and fills it |
| `c_menubar_dicts.gspec` | Menu Dicts | C | a menubar whose `menus` are `{title, items}` dicts, with a separator and a nested submenu | the window's menu bar has File / Edit / Help; File > Open and Edit > Transform > Upper case each reach `on_menu` with their label; the text beneath keeps at least 0.75 of its drawn size (measured 1074x613). It got 356 of 1100 px until the menubar's grid bands stopped staying elastic after `setMenuBar` takes it out of the grid |
| `c_menubar_titles.gspec` | Menu Titles | C | a menubar whose `menus` is a bare list of strings (once "'str' object has no attribute 'get'" at emit) | builds; three empty menus File / View / Help; the button still reaches its handler |
| `c_float_scale.gspec` | Float Scale | C | a scale with `from_` 0.5, `to` 9.5 and a port default of 2.5 — bends the whole-number rule on purpose | builds, degraded: QSlider holds whole numbers, so the range is 0..9 and the value 2, and `ui/main_ui.py` says both bounds were truncated |
| `c_colour_no_caps.gspec` | Colour Ignored | C | bg/fg on eight kinds that cannot take colour (combobox, progress bar, table, file picker, toolbar, notebook, scrubber, status bar) under a coloured window | builds; those colours are dropped (no stylesheet, `#ff0000` nowhere in the UI), while the one colourable label keeps its own; the window opens at the drawn 1100x700 (it opened 1100x780 while the empty notebook's drawn size was a hard `setMinimumSize`; it is now a size hint). **Known Qt loss (strict xfail):** the empty notebook Empty tabs, drawn 544x360, gets 199x280 at 1100x700 where Tk gives 563x309 — QGridLayout ignores a size hint in a stretched column, and gui_layout made its column one of seven elastic ones |
