"""
council_qt — the Qt front end for Data's Inferno.

THIS PACKAGE HOLDS UI ONLY. The application's logic stays where it is and is
imported by both front ends; nothing in here is a copy of anything in
council_gui_engine.py. That rule is the whole reason a parallel front end is
affordable: measured, only ~3,900 of the app's ~32,000 lines are toolkit-bound,
and 135 logic-shaped methods (7,264 lines) reach the UI through just 34 entry
points — 76% of those through three seams (append text, schedule on the UI
thread, post to the queue).

Copy the logic in here instead and the project forks: every fix to the shipping
Tk app would have to be made twice for however many months the port takes, which
is the usual way a rewrite dies.

The Tk shell keeps running, keeps shipping, and stays the reference
implementation until the last phase. Tk and Qt cannot share a process — a
QApplication flips the process to per-monitor DPI awareness and an open Tk
window shrinks ~20% on the spot — so the two front ends are two entry points,
never two windows at once.

See docs/qt_full_port_scope.md for the phases and what is done.
"""
from __future__ import annotations

__all__ = ["bridge", "dialogs", "theme", "window"]
