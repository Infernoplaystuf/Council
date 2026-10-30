# Example Qt apps

Ten complete PySide6 apps the Council's models read when they write Qt code.
Each is one file, runs on its own, and passes the same policy gate a
generated app must pass (`gui_policy.validate(mode="standalone",
toolkit="qt")` with only its declared `REQUIRES`), so a pattern copied from
one is a pattern the gate allows.

The limit is ten. Replace an example rather than adding an eleventh.

| App | What it is | Main widgets and patterns |
|---|---|---|
| `csv_plotter.py` | One CSV or a folder of them, as line, scatter, bar, area, step, histogram, box, violin or correlation heatmap | matplotlib `FigureCanvasQTAgg` + toolbar, `QDockWidget`, checkable `QListWidget`, debounce `QTimer`, drag and drop |
| `local_chatbot.py` | Chat with an Ollama model or a `.gguf` file (llama-cpp-python), or the built-in Echo | worker `QObject` on a `QThread`, streaming through Signals, Stop, lazy optional imports, `QTextBrowser` bubbles |
| `table_editor.py` | Open, filter, sort and edit a CSV with undo | `QAbstractTableModel`, `QSortFilterProxyModel`, `QStyledItemDelegate`, `QUndoStack`, clipboard copy/paste |
| `file_browser.py` | Browse folders and preview text and images (read-only) | `QFileSystemModel` in a tree and a table, `QCompleter`, back/forward history, `QStackedWidget` preview |
| `settings_dialog.py` | Preferences with nearly every input widget, saved as JSON | `QDialog`, `QDialogButtonBox` (Apply/Restore), validators and input masks, `QDial`, `QKeySequenceEdit`, `QColorDialog`, themes with `QPalette` |
| `image_viewer.py` | Zoom, pan and step through a folder of images; draw and save a region | `QGraphicsView`/`Scene`, wheel zoom, rubber-band region, `QTransform`, custom histogram widget, lazy thumbnails |
| `job_runner.py` | Many background jobs with progress, cancel and a log | `QThreadPool` + `QRunnable`, signals on a helper `QObject`, `threading.Event` cancel, progress bars in a `QTreeWidget` |
| `live_dashboard.py` | Live gauges, sparklines, status lights and alarms | custom `paintEvent` widgets, `Property` + `QPropertyAnimation`, style sheets, light/dark themes |
| `text_editor.py` | Tabbed code editor with line numbers, highlighting, find/replace | full `QMainWindow` menus/toolbar, line-number area, `QSyntaxHighlighter`, `QTextDocument.find`, recent files |
| `setup_wizard.py` | A "new project" wizard that branches, validates and then does the work | `QWizard`, `registerField`, `isComplete`/`validatePage`/`nextId`, checkable tristate tree, `QCalendarWidget`, commit page |

## Running one

```
python examples/qt_apps/csv_plotter.py assets/sample_data
python examples/qt_apps/local_chatbot.py
```

`csv_plotter` needs pandas, numpy and matplotlib; `image_viewer` needs numpy;
the chatbot's Ollama backend needs `pip install ollama` and its GGUF backend
`pip install llama-cpp-python`. Everything else needs only PySide6.

## How the Council uses them

`gui_reference.py` reads them as text. It picks the example that best
matches a request (`select`, `for_prompt`) from each file's docstring and a
short list of everyday words per example.

In the Build tab's Agent mode, the built-in **Qt app builder** agent has this folder as a
reference: the best-matching example goes into its context whole, and its
`search_references` tool finds passages from the others.

## Rules every example follows

These are the rules that a generated app has to follow too. Each one was a
gate refusal or a frozen window before it became a rule.

* Only `QtCore`, `QtGui` and `QtWidgets`. No `QtNetwork`, `QtSql`,
  `QtCharts`, `QtPrintSupport`. Charts are matplotlib's Qt canvas.
* No `QSettings`, because it writes the Windows registry. Settings are JSON
  in `QStandardPaths.writableLocation(AppConfigLocation)`, written through a
  temporary file.
* No `QProcess`, `QDesktopServices`, `subprocess`, `urllib`, `socket`, and
  nothing that deletes files.
* The gate refuses some attribute names whatever object they are on, so a
  few ordinary spellings are avoided: `.remove(...)` (`list.remove(x)`
  included: use `del` or a comprehension), `.load(...)` and `.loads(...)`
  except on `json` and the other safe readers (`QPixmap(path)`, not
  `pixmap.load(path)`), and attributes named `system`, `modules`, `kill` or
  `unlink` (`self.system_prompt`, not `self.system`).
* Work that takes more than about 50 ms runs off the UI thread, on a
  `QThread` worker or a `QThreadPool`, and reports back only through Signals.
  It is cancelled with a flag the worker checks.
* An optional package is imported where it is used, and a missing one is
  reported in the window, not by a crash at start.
* Every file has `make_window(argv=None)` returning the top-level window
  without calling `exec()`, so a test can build it offscreen, and a
  `main()`.
* The docstring has the same headings in the same order: a title line,
  WHAT IT DOES, SHOWS, RUN, REQUIRES. `gui_reference` reads them.
