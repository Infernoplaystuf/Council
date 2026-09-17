"""
council_core — application logic that belongs to neither front end.

WHY THIS PACKAGE EXISTS
The Council is being given a second front end (council_qt/) while the first one
(council_gui_engine.py) keeps shipping. The only way that is affordable is if the
logic is shared rather than copied: measured, ~4,500 of the app's ~35,000 lines
are toolkit-bound, so a copy would fork ~30,000 lines of working code and force
every fix to be made twice for as long as the port takes.

So each phase of the port moves that area's logic here FIRST, in place, with the
Tk shell still calling it and its tests still passing — and only then writes the
Qt view against the same functions.

THE RULE FOR EVERYTHING IN HERE
No tkinter. No PySide6. No widgets, no variables, no scheduling. A function here
reports progress by CALLING A CALLBACK the caller supplied, and the caller
decides which thread that lands on. The repo already enforces this shape on the
gui_* modules with ten tests that parse the source and fail on a toolkit import;
council_core is held to the same rule in tests/test_council_core.py.
"""
from __future__ import annotations

__all__ = ["vault_ops"]
